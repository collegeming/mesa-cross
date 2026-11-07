/* SPDX-License-Identifier: GPL-3.0-or-later
 * Copyright (C) 2026 ASEbrahim and MaliBridge contributors
 *
 * mbctl - the container side of the MaliBridge budget control socket.
 *
 *   mbctl status [--json]
 *   mbctl set --total MB [--pid MB] [--floor MB] [--map N] [--persist] [--json]
 *   mbctl budget TOTAL [PID [FLOOR]] [--persist] [--json]
 *
 * MB values take a plain number, an M/MB or a G/GB suffix ("4096", "1.5G").
 * Connects to /opt/mb-sock/control.sock (MBCTL_SOCKET or --socket), waits at most
 * 2 s to connect and 2 s for the reply. Exit codes: 0 ok, 1 usage, 2 connect,
 * 3 timeout, 4 protocol, 5 server refused, 6 closed without a reply (peer uid).
 */
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <poll.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/un.h>
#include <time.h>
#include <unistd.h>

#define SOCK_DEFAULT "/opt/mb-sock/control.sock"
#define LINE_MAX_B 8192
#define TIMEOUT_MS 2000

static int64_t now_ms(void)
{
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (int64_t)ts.tv_sec * 1000 + ts.tv_nsec / 1000000;
}

static void usage(FILE *f)
{
    fprintf(f,
            "usage: mbctl [--socket PATH] status [--json]\n"
            "       mbctl [--socket PATH] set --total MB [--pid MB] [--floor MB] [--map N] [--persist] [--json]\n"
            "       mbctl [--socket PATH] budget TOTAL [PID [FLOOR]] [--persist] [--json]\n"
            "\n"
            "MB values: 4096, 4096M, 4G, 1.5G. budget takes GB without a suffix:\n"
            "TOTAL presets 2/3/4/6/8, PID 1/2/3/4, FLOOR 1.5/2/2.5/3\n"
            "(defaults: PID 2G or TOTAL/2 below 4G, FLOOR 2G).\n"
            "The socket is %s or $MBCTL_SOCKET; --socket overrides it.\n",
            SOCK_DEFAULT);
}

static int parse_mb(const char *s, int64_t *out)
{
    char *end = NULL;
    errno = 0;
    double v = strtod(s, &end);
    if (end == s || errno || v < 0) return -1;
    while (*end == ' ' || *end == '\t') end++;
    if (*end == 'G' || *end == 'g') {
        if (end[1] && strcasecmp(end + 1, "b")) return -1;
        v *= 1024.0;
    } else if (*end == 'M' || *end == 'm') {
        if (end[1] && strcasecmp(end + 1, "b")) return -1;
    } else if (*end) {
        return -1;
    }
    if (v > 1048576.0) return -1;
    *out = (int64_t)(v + 0.5);
    return 0;
}

static int parse_u64(const char *s, int64_t *out)
{
    char *end = NULL;
    errno = 0;
    long long v = strtoll(s, &end, 10);
    if (end == s || *end || errno || v < 0) return -1;
    *out = (int64_t)v;
    return 0;
}

/* The budget presets are written as 2/3/4/6/8 (GB), 1/2/3/4 (GB) and 1.5/2/2.5/3 (GB);
 * an explicit M or G suffix is honoured. */
static int parse_gb(const char *s, int64_t *out)
{
    if (strpbrk(s, "GgMm")) return parse_mb(s, out);
    char *end = NULL;
    errno = 0;
    double v = strtod(s, &end);
    if (end == s || *end || errno || v < 0 || v > 32) return -1;
    *out = (int64_t)(v * 1024.0 + 0.5);
    return 0;
}

static int connect_sock(const char *path, int *err)
{
    struct sockaddr_un a;
    if (strlen(path) >= sizeof(a.sun_path)) {
        *err = ENAMETOOLONG;
        return -1;
    }
    int fd = socket(AF_UNIX, SOCK_STREAM | SOCK_CLOEXEC, 0);
    if (fd < 0) {
        *err = errno;
        return -1;
    }
    int fl = fcntl(fd, F_GETFL, 0);
    if (fl < 0 || fcntl(fd, F_SETFL, fl | O_NONBLOCK) < 0) {
        *err = errno;
        close(fd);
        return -1;
    }
    memset(&a, 0, sizeof(a));
    a.sun_family = AF_UNIX;
    strcpy(a.sun_path, path);
    if (connect(fd, (struct sockaddr *)&a, sizeof(a)) == 0) return fd;
    if (errno != EINPROGRESS) {
        *err = errno;
        close(fd);
        return -1;
    }
    struct pollfd pfd = {.fd = fd, .events = POLLOUT};
    int pr = poll(&pfd, 1, TIMEOUT_MS);
    if (pr <= 0) {
        *err = pr == 0 ? ETIMEDOUT : errno;
        close(fd);
        return -1;
    }
    int soerr = 0;
    socklen_t sl = sizeof(soerr);
    if (getsockopt(fd, SOL_SOCKET, SO_ERROR, &soerr, &sl) < 0 || soerr) {
        *err = soerr ? soerr : errno;
        close(fd);
        return -1;
    }
    return fd;
}

static int write_all(int fd, const char *s, size_t n)
{
    int64_t deadline = now_ms() + TIMEOUT_MS;
    while (n) {
        int64_t left = deadline - now_ms();
        if (left <= 0) return 3;
        struct pollfd pfd = {.fd = fd, .events = POLLOUT};
        int pr = poll(&pfd, 1, (int)left);
        if (pr < 0 && errno == EINTR) continue;
        if (pr <= 0) return 3;
        ssize_t w = send(fd, s, n, MSG_NOSIGNAL);
        if (w < 0 && errno == EINTR) continue;
        if (w <= 0) return 4;
        s += w;
        n -= (size_t)w;
    }
    return 0;
}

/* Returns 0 with a NUL-terminated line in buf, 3 on timeout, 4 on a broken reply,
 * 6 when the peer closed before sending anything (SO_PEERCRED refusal). */
static int read_line(int fd, char *buf, size_t cap, size_t *len)
{
    int64_t deadline = now_ms() + TIMEOUT_MS;
    *len = 0;
    for (;;) {
        int64_t left = deadline - now_ms();
        if (left <= 0) return 3;
        struct pollfd pfd = {.fd = fd, .events = POLLIN};
        int pr = poll(&pfd, 1, (int)left);
        if (pr < 0 && errno == EINTR) continue;
        if (pr <= 0) return 3;
        ssize_t k = recv(fd, buf + *len, cap - 1 - *len, 0);
        if (k < 0 && errno == EINTR) continue;
        if (k < 0) return *len ? 4 : ((errno == ECONNRESET || errno == EPIPE) ? 6 : 4);
        if (k == 0) return *len ? 4 : 6;
        *len += (size_t)k;
        buf[*len] = 0;
        if (memchr(buf, '\n', *len)) return 0;
        if (*len + 1 >= cap) return 4;
    }
}

static const char *jnum(const char *json, const char *key, int64_t *out)
{
    char pat[64];
    snprintf(pat, sizeof(pat), "\"%s\":", key);
    const char *p = strstr(json, pat);
    if (!p) return NULL;
    p += strlen(pat);
    char *end = NULL;
    long long v = strtoll(p, &end, 10);
    if (end == p) return NULL;
    *out = (int64_t)v;
    return end;
}

static int jstr(const char *json, const char *key, char *out, size_t cap)
{
    char pat[64];
    snprintf(pat, sizeof(pat), "\"%s\":\"", key);
    const char *p = strstr(json, pat);
    if (!p) return -1;
    p += strlen(pat);
    size_t n = 0;
    while (*p && *p != '"' && n + 1 < cap) {
        char c = *p++;
        if (c == '\\' && *p) {
            c = *p++;
            if (c == 'n') c = '\n';
            else if (c == 't') c = '\t';
        }
        out[n++] = c;
    }
    out[n] = 0;
    return 0;
}

static void print_status(const char *json, int as_json)
{
    if (as_json) {
        printf("%s\n", json);
        return;
    }
    int64_t total_mb = 0, pid_mb = 0, floor_mb = 0, map_limit = 0, used = 0, count = 0, avail = 0, maps = 0, rss = 0;
    jnum(json, "total_mb", &total_mb);
    jnum(json, "pid_mb", &pid_mb);
    jnum(json, "floor_mb", &floor_mb);
    jnum(json, "map_limit", &map_limit);
    jnum(json, "total_bytes", &used);
    jnum(json, "clients_count", &count);
    jnum(json, "mem_available_kb", &avail);
    jnum(json, "maps", &maps);
    jnum(json, "server_rss_kb", &rss);
    printf("budget  total %lld MB, per client %lld MB, MemAvailable floor %lld MB, map limit %lld\n", (long long)total_mb,
           (long long)pid_mb, (long long)floor_mb, (long long)map_limit);
    printf("usage   %.1f MB total, %lld client%s\n", (double)used / 1048576.0, (long long)count, count == 1 ? "" : "s");
    const char *p = strstr(json, "\"clients\":[");
    if (p) {
        p += strlen("\"clients\":[");
        while (*p) {
            p = strstr(p, "{\"pid\":");
            if (!p) break;
            int64_t pid = 0, bytes = 0;
            const char *q = jnum(p, "pid", &pid);
            if (!q || !jnum(p, "bytes", &bytes)) break;
            printf("        pid %-8lld %.1f MB\n", (long long)pid, (double)bytes / 1048576.0);
            p = q;
            if (*p == '}') p++;
            if (*p == ']') break;
        }
    }
    printf("system  MemAvailable %lld MB, maps %lld, server RSS %lld MB\n", (long long)(avail >> 10), (long long)maps,
           (long long)(rss >> 10));
}

int main(int argc, char **argv)
{
    const char *sock = getenv("MBCTL_SOCKET");
    if (!sock || !*sock) sock = SOCK_DEFAULT;
    int as_json = 0, i = 1;
    while (i < argc) {
        if (!strcmp(argv[i], "--socket") && i + 1 < argc) sock = argv[++i];
        else if (!strcmp(argv[i], "--json")) as_json = 1;
        else if (!strcmp(argv[i], "-h") || !strcmp(argv[i], "--help")) {
            usage(stdout);
            return 0;
        } else {
            break;
        }
        i++;
    }
    if (i >= argc) {
        usage(stderr);
        return 1;
    }
    const char *cmd = argv[i++];
    char req[768];
    int n = 0;

    if (!strcmp(cmd, "status")) {
        for (; i < argc; i++) {
            if (!strcmp(argv[i], "--json")) as_json = 1;
            else {
                usage(stderr);
                return 1;
            }
        }
        n = snprintf(req, sizeof(req), "{\"cmd\":\"status\"}\n");
    } else if (!strcmp(cmd, "set")) {
        int has_total = 0, has_pid = 0, has_floor = 0, has_map = 0, persist = 0;
        int64_t total = 0, pid = 0, floor = 0, map = 0;
        for (; i < argc; i++) {
            if (!strcmp(argv[i], "--total") && i + 1 < argc) {
                if (parse_mb(argv[++i], &total)) goto bad_value;
                has_total = 1;
            } else if (!strcmp(argv[i], "--pid") && i + 1 < argc) {
                if (parse_mb(argv[++i], &pid)) goto bad_value;
                has_pid = 1;
            } else if (!strcmp(argv[i], "--floor") && i + 1 < argc) {
                if (parse_mb(argv[++i], &floor)) goto bad_value;
                has_floor = 1;
            } else if (!strcmp(argv[i], "--map") && i + 1 < argc) {
                if (parse_u64(argv[++i], &map)) goto bad_value;
                has_map = 1;
            } else if (!strcmp(argv[i], "--persist")) {
                persist = 1;
            } else if (!strcmp(argv[i], "--json")) {
                as_json = 1;
            } else {
                usage(stderr);
                return 1;
            }
        }
        if (!has_total && !has_pid && !has_floor && !has_map) {
            fprintf(stderr, "mbctl: set needs at least one of --total, --pid, --floor, --map\n");
            return 1;
        }
        n = snprintf(req, sizeof(req), "{\"cmd\":\"set\"");
        if (has_total) n += snprintf(req + n, sizeof(req) - (size_t)n, ",\"total_mb\":%lld", (long long)total);
        if (has_pid) n += snprintf(req + n, sizeof(req) - (size_t)n, ",\"pid_mb\":%lld", (long long)pid);
        if (has_floor) n += snprintf(req + n, sizeof(req) - (size_t)n, ",\"floor_mb\":%lld", (long long)floor);
        if (has_map) n += snprintf(req + n, sizeof(req) - (size_t)n, ",\"map_limit\":%lld", (long long)map);
        if (persist) n += snprintf(req + n, sizeof(req) - (size_t)n, ",\"persist\":true");
        n += snprintf(req + n, sizeof(req) - (size_t)n, "}\n");
    } else if (!strcmp(cmd, "budget")) {
        int64_t vals[3] = {0, 0, 0};
        int nv = 0, persist = 0;
        for (; i < argc; i++) {
            if (!strcmp(argv[i], "--persist")) persist = 1;
            else if (!strcmp(argv[i], "--json")) as_json = 1;
            else if (nv < 3) {
                if (parse_gb(argv[i], &vals[nv])) goto bad_value;
                nv++;
            } else {
                usage(stderr);
                return 1;
            }
        }
        if (nv < 1) {
            usage(stderr);
            return 1;
        }
        int64_t total = vals[0], pid = nv >= 2 ? vals[1] : (total >= 4096 ? 4096 : 2048);
        int64_t floor = nv >= 3 ? vals[2] : 2048;
        if (!(total == 2048 || total == 3072 || total == 4096 || total == 6144 || total == 8192)) {
            fprintf(stderr, "mbctl: TOTAL presets are 2G, 3G, 4G, 6G, 8G\n");
            return 1;
        }
        if (!(pid == 1024 || pid == 2048 || pid == 3072 || pid == 4096)) {
            fprintf(stderr, "mbctl: PID presets are 1G, 2G, 3G, 4G\n");
            return 1;
        }
        if (!(floor == 1536 || floor == 2048 || floor == 2560 || floor == 3072)) {
            fprintf(stderr, "mbctl: FLOOR presets are 1.5G, 2G, 2.5G, 3G\n");
            return 1;
        }
        n = snprintf(req, sizeof(req), "{\"cmd\":\"set\",\"total_mb\":%lld,\"pid_mb\":%lld,\"floor_mb\":%lld%s}\n",
                     (long long)total, (long long)pid, (long long)floor, persist ? ",\"persist\":true" : "");
    } else {
        usage(stderr);
        return 1;
    }

    int cerr = 0;
    int fd = connect_sock(sock, &cerr);
    if (fd < 0) {
        fprintf(stderr, "mbctl: connect %s: %s\n", sock, strerror(cerr));
        return 2;
    }
    int rc = write_all(fd, req, (size_t)n);
    if (rc) {
        fprintf(stderr, "mbctl: send: %s\n", rc == 3 ? "timeout" : "connection failed");
        close(fd);
        return rc;
    }
    char reply[LINE_MAX_B];
    size_t len = 0;
    rc = read_line(fd, reply, sizeof(reply), &len);
    close(fd);
    if (rc == 3) {
        fprintf(stderr, "mbctl: no reply within %d ms\n", TIMEOUT_MS);
        return 3;
    }
    if (rc == 6) {
        fprintf(stderr, "mbctl: server closed the connection without a reply (uid not allowed?)\n");
        return 6;
    }
    if (rc) {
        fprintf(stderr, "mbctl: malformed or truncated reply\n");
        return 4;
    }
    while (len && (reply[len - 1] == '\n' || reply[len - 1] == '\r')) reply[--len] = 0;
    if (!strstr(reply, "\"ok\":true")) {
        char err[512] = "";
        jstr(reply, "error", err, sizeof(err));
        fprintf(stderr, "mbctl: server refused: %s\n", err[0] ? err : reply);
        if (as_json) printf("%s\n", reply);
        return 5;
    }
    if (!strcmp(cmd, "status")) {
        print_status(reply, as_json);
    } else if (as_json) {
        printf("%s\n", reply);
    } else {
        char warn[512] = "";
        jstr(reply, "warning", warn, sizeof(warn));
        if (warn[0]) printf("ok (warning: %s)\n", warn);
        else printf("ok\n");
    }
    return 0;

bad_value:
    fprintf(stderr, "mbctl: bad number; use 4096, 4096M, 4G or 1.5G\n");
    return 1;
}
