/**
 * The services on ESP-IDF's Linux host target: esp_timer, a FreeRTOS critical
 * section, and a FreeRTOS task over a non-blocking socket.
 *
 * The rules of that port apply, and each one is a way it breaks:
 *
 * - no FreeRTOS task blocks in a host system call. The port only knows a task
 *   is blocked when it blocked on a FreeRTOS primitive; a task sitting in
 *   recv() is, to the scheduler, the running task. So the socket is
 *   non-blocking and the reader's only wait is select() with no timeout,
 *   which is interposed for a task: hw-linux blocks the task until the socket
 *   is readable, and IDF's own interposer, where no board replaces it, polls
 *   and sleeps on a delay;
 * - every task stack is at least 20 KB and has no core affinity: a task is a
 *   pthread with a real mapping, and there is one core;
 * - one critical section for everything. On this port it nests (a
 *   per-thread signal mask with a global count), which is the recursive lock
 *   the model needs, and contention between chips is nil.
 *
 * Logging goes to ESP-IDF's log under the tag `simradio`, which is where the
 * firmware's own logger reads everything else.
 */
#include "services.h"

#include "conductor.h"
#include "simradio.h"

#include "esp_log.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

#include <arpa/inet.h>
#include <cerrno>
#include <cstdarg>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <fcntl.h>
#include <netinet/in.h>
#include <sys/select.h>
#include <sys/socket.h>
#include <sys/syscall.h>
#include <time.h>
#include <unistd.h>

/* The board's identity, and the one call it takes back from the clock. Weak:
 * a project may link this backend without the board. */
extern "C" __attribute__((weak)) int hwLinuxNodeId(void) { return 1; }
extern "C" __attribute__((weak)) const char* hwLinuxBindAddr(void) { return "127.0.0.1"; }
extern "C" __attribute__((weak)) const char* hwLinuxEtherAddr(void) { return ""; }
extern "C" void hwLinuxClockDue(void) __attribute__((weak));
extern "C" void hwLinuxClockMoved(void) __attribute__((weak));

namespace {

constexpr int      kMaxDatagram = 4096;
constexpr uint32_t kTaskStack   = 32768;
constexpr int      kTaskPrio    = 6;      /* above a radio task at 5 */

const char* const kTag = "simradio";

portMUX_TYPE s_mux = portMUX_INITIALIZER_UNLOCKED;

/* The host's monotonic clock, read past any time shim and never through
 * esp_timer, which reads this library. Zero at the first reading. */
int64_t nowUs()
{
    static int64_t origin = -1;
    struct timespec ts;
    syscall(SYS_clock_gettime, CLOCK_MONOTONIC, &ts);
    int64_t now = (int64_t)ts.tv_sec * 1000000 + ts.tv_nsec / 1000;
    if (origin < 0) origin = now;
    return now - origin;
}

void* timerCreate(void (*cb)(void*), void* arg, const char* name)
{
    esp_timer_create_args_t a = {};
    a.callback = cb;
    a.arg = arg;
    a.name = name;
    esp_timer_handle_t h = nullptr;
    if (esp_timer_create(&a, &h) != ESP_OK) return nullptr;
    return h;
}

void timerStartOnce(void* timer, int64_t delayUs)
{
    if (!timer) return;
    auto h = (esp_timer_handle_t)timer;
    esp_timer_stop(h);                  /* restart, not "already running" */
    esp_timer_start_once(h, (uint64_t)(delayUs < 0 ? 0 : delayUs));
}

void timerStop(void* timer)
{
    if (timer) esp_timer_stop((esp_timer_handle_t)timer);
}

void lock() { portENTER_CRITICAL(&s_mux); }
void unlock() { portEXIT_CRITICAL(&s_mux); }

void logLine(int level, const char* fmt, ...)
{
    esp_log_level_t l = level <= SIMRADIO_LOG_ERROR ? ESP_LOG_ERROR
                      : level == SIMRADIO_LOG_WARN  ? ESP_LOG_WARN
                                                    : ESP_LOG_INFO;
    char line[256];
    va_list ap;
    va_start(ap, fmt);
    vsnprintf(line, sizeof line, fmt, ap);
    va_end(ap);
    ESP_LOG_LEVEL(l, kTag, "%s", line);     /* the prefix and the newline are the log's */
}

int udpOpen(const char* bindAddr, const char* dest)
{
    char host[96];
    snprintf(host, sizeof host, "%s", dest ? dest : "");
    char* colon = strrchr(host, ':');
    if (!colon) {
        logLine(SIMRADIO_LOG_ERROR, "ether: %s is not host:port", host);
        return -1;
    }
    *colon = '\0';
    int port = atoi(colon + 1);

    int fd = socket(AF_INET, SOCK_DGRAM, 0);
    if (fd < 0) {
        logLine(SIMRADIO_LOG_ERROR, "ether: socket: %s", strerror(errno));
        return -1;
    }
    fcntl(fd, F_SETFL, fcntl(fd, F_GETFL, 0) | O_NONBLOCK);
    /* Room for a burst of the ether's messages: one the kernel drops is
     * recovered only by a resend (conductor::resendIdle). */
    int rcvbuf = kRecvBufferBytes;
    setsockopt(fd, SOL_SOCKET, SO_RCVBUF, &rcvbuf, sizeof rcvbuf);

    /* Bound to an ephemeral port on this station's own address, so the ether
     * can tell one station's datagrams from another's by source alone. */
    struct sockaddr_in local = {};
    local.sin_family = AF_INET;
    local.sin_addr.s_addr = inet_addr(bindAddr);
    local.sin_port = 0;
    if (bind(fd, (struct sockaddr*)&local, sizeof local) != 0)
        logLine(SIMRADIO_LOG_WARN, "ether: bind %s: %s", bindAddr, strerror(errno));

    struct sockaddr_in peer = {};
    peer.sin_family = AF_INET;
    peer.sin_addr.s_addr = inet_addr(host);
    peer.sin_port = htons((uint16_t)port);
    if (connect(fd, (struct sockaddr*)&peer, sizeof peer) != 0) {
        logLine(SIMRADIO_LOG_ERROR, "ether: connect %s: %s", dest, strerror(errno));
        close(fd);
        return -1;
    }
    return fd;
}

struct Reader {
    int fd;
    void (*onDatagram)(const char*, size_t);
};

void readerTask(void* arg)
{
    Reader r = *(Reader*)arg;
    delete (Reader*)arg;
    static char buf[kMaxDatagram + 1];
    for (;;) {
        fd_set rd;
        FD_ZERO(&rd);
        FD_SET(r.fd, &rd);
        if (select(r.fd + 1, &rd, nullptr, nullptr, nullptr) <= 0) continue;
        for (;;) {
            ssize_t n = recv(r.fd, buf, kMaxDatagram, MSG_DONTWAIT);
            if (n <= 0) break;
            buf[n] = '\0';
            r.onDatagram(buf, (size_t)n);
        }
    }
}

int spawnReader(int fd, void (*onDatagram)(const char*, size_t))
{
    auto* r = new Reader{fd, onDatagram};
    TaskHandle_t h = nullptr;
    if (xTaskCreatePinnedToCore(readerTask, "ether", kTaskStack, r, kTaskPrio, &h,
                                tskNO_AFFINITY) != pdPASS) {
        delete r;
        return -1;
    }
    return 0;
}

const struct simradio_services kServices = {
    nowUs,
    timerCreate,
    timerStartOnce,
    timerStop,
    lock,
    unlock,
    udpOpen,
    spawnReader,
    logLine,
};

/* esp_timer's next expiry, as a wake in node time. */
int s_timerWake = -1;

void timerDue(void*)
{
    if (hwLinuxClockDue) hwLinuxClockDue();
}

/* The next tick a task waits for, as a wake in node time. Reaching it does
 * nothing of its own: every move of T already brings the tick count up. */
int s_tickWake = -1;

void tickDue(void*) {}

void clockMoved()
{
    if (hwLinuxClockMoved) hwLinuxClockMoved();
}

/* The station's clock reads 0 at the whole second of node time the station
 * joined in. Every station's clock is then a whole number of seconds from
 * every other's, so what the board times on it — its tick above all — falls
 * on the same instants of T on every station, and those share a barrier. */
constexpr int64_t kSecondUs = 1000000;

int64_t clockZero()
{
    int64_t join = conductor::nodeAtJoin();
    return join - join % kSecondUs;
}

int64_t toNode(int64_t us)
{
    return us == conductor::kNever ? conductor::kNever : us + clockZero();
}

}  // namespace

extern "C" const struct simradio_services* simradio_services(void)
{
    return &kServices;
}

/* ---- The board's clock (hw-linux's hwlinux.h) ----
 *
 * esp_timer, the board's tick and idle, and its bring-up reach the station's
 * clock through these. In a real-time run the clock is the host's and this
 * adds nothing; in a virtual one esp_timer counts node time from the ether's
 * welcome, its next expiry and the next tick a task waits for are wakes, the
 * tick count follows every move of T, and the idle task is what tells the
 * ether this station is idle. */

extern "C" int64_t hwLinuxClockUs(void)
{
    if (!conductor::isVirtual()) return nowUs();
    return conductor::joined() ? conductor::nodeNowUs() - clockZero() : 0;
}

extern "C" void hwLinuxClockWake(int64_t us)
{
    if (!conductor::isVirtual()) return;
    if (s_timerWake < 0) s_timerWake = conductor::wakeCreate(timerDue, nullptr);
    conductor::wakeAt(s_timerWake, toNode(us));
}

extern "C" void hwLinuxClockTickAt(int64_t us)
{
    if (!conductor::isVirtual()) return;
    if (s_tickWake < 0) s_tickWake = conductor::wakeCreate(tickDue, nullptr);
    conductor::wakeAt(s_tickWake, toNode(us));
}

extern "C" void hwLinuxClockIdle(void)
{
    conductor::idle();
}

/* A virtual run needs the ether before anything in the station waits on
 * time, since only the ether moves it: so the link opens here, at the
 * board's bring-up, rather than when the radio does. */
extern "C" int hwLinuxClockStart(void)
{
    if (!conductor::isVirtual()) return 0;
    conductor::onAdvance(clockMoved);
    simradio_station_open(hwLinuxNodeId(), hwLinuxBindAddr(), hwLinuxEtherAddr());
    return 1;
}
