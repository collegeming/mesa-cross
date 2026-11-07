#!/usr/bin/env python3
"""Apply/check the isolated 47ec18e server-side memory budget candidate.

Only server/server_memory.c, server/server_instance.c, server/server_main.c,
server/vkb_server.h and common/gen/vkb_gen_server.c change. The generated
server dispatch is patched in place because CMake builds the checked-in copy
(codegen is not run by the build).
"""
import argparse
import hashlib
import subprocess
from pathlib import Path

REVISION = "47ec18eaa9d9f83c359d56ba412a17d61ba59a66"
BLOBS = {
    "server/server_memory.c": "cb8578f8610436f5501d2d2d8836431be685f7c8",
    "server/server_instance.c": "823c9c08a4a9ff58c7a6c777c400d3464e16b18c",
    "server/server_main.c": "a9d2359a19c64e556c52b77015ed490ebbd8f31c",
    "server/vkb_server.h": "9a8971817daff04f09602ca6d0a5f6fc82c3b9a6",
    "common/gen/vkb_gen_server.c": "ea6923f2617e10136bf2c84bbbde0993e441a6cc",
}


def git(root, *args):
    return subprocess.check_output(["git", "-C", str(root), *args], text=True).strip()


def canonical(data):
    return data.replace(b"\r\n", b"\n")


def blob_hash(data):
    data = canonical(data)
    return hashlib.sha1(b"blob " + str(len(data)).encode() + b"\0" + data).hexdigest()


BUDGET_C = r'''
/* ------------------------------------------------------------------ memory budget */

/* Checked before a client's allocation reaches the driver, and before a new device or a big image
 * is made: over a limit the call fails with VK_ERROR_OUT_OF_DEVICE_MEMORY. Limits in MB, 0: off;
 * VKBRIDGE_MAP_LIMIT is a count of /proc/self/maps lines:
 *   VKBRIDGE_MEM_BUDGET_PID_MB     one client process (default 1024 MB)
 *   VKBRIDGE_MEM_BUDGET_TOTAL_MB   all clients together (default 2048 MB)
 *   VKBRIDGE_MEM_MIN_AVAIL_MB      MemAvailable floor for a new device or a big allocation (512 MB)
 *   VKBRIDGE_MAP_LIMIT             mappings over which those are refused (50000)
 * The per-client figure is the process's VkDeviceMemory (the report's "pids"); decoded BC images,
 * which the driver allocates beneath it, count in the total (st.img_bc) but not per client. */
#define BUDGET_PID_DEFAULT_MB 1024
#define BUDGET_TOTAL_DEFAULT_MB 2048
#define BUDGET_AVAIL_DEFAULT_MB 512
#define BUDGET_MAPS_DEFAULT 50000
#define BUDGET_BIG ((VkDeviceSize)64 << 20)

typedef struct { int pid; int64_t bytes; } budget_res;
static struct {
    int64_t pid_bytes, total_bytes, avail_floor_kb, map_limit;
    int64_t reserved;              /* bytes an allocation in flight has reserved */
    budget_res res[ST_PIDS];
    uint64_t avail_kb;
    int64_t maps, avail_at, maps_at;
} budget;
static pthread_once_t budget_once = PTHREAD_ONCE_INIT;
static int budget_denials;

static int64_t budget_env_num(const char *name, int64_t def)
{
    const char *v = vkb_srv_env_global(name);
    if (!v || !*v) return def;
    char *end = NULL;
    long long n = strtoll(v, &end, 10);
    if (end == v || *end || n < 0) {
        VKB_WARN("%s=%.60s is not a number; %lld used", name, v, (long long)def);
        return def;
    }
    return (int64_t)n;
}

static int64_t budget_mb_bytes(int64_t mb)
{
    if (mb > (INT64_MAX >> 20)) return INT64_MAX;
    return mb << 20;
}

static void budget_init(void)
{
    budget.pid_bytes = budget_mb_bytes(budget_env_num("VKBRIDGE_MEM_BUDGET_PID_MB", BUDGET_PID_DEFAULT_MB));
    budget.total_bytes = budget_mb_bytes(budget_env_num("VKBRIDGE_MEM_BUDGET_TOTAL_MB", BUDGET_TOTAL_DEFAULT_MB));
    budget.avail_floor_kb = budget_mb_bytes(budget_env_num("VKBRIDGE_MEM_MIN_AVAIL_MB", BUDGET_AVAIL_DEFAULT_MB)) >> 10;
    budget.map_limit = budget_env_num("VKBRIDGE_MAP_LIMIT", BUDGET_MAPS_DEFAULT);
    VKB_INFO("memory budget: per client %lld MB, all clients %lld MB, MemAvailable floor %lld MB, map limit %lld%s",
             (long long)(budget.pid_bytes >> 20), (long long)(budget.total_bytes >> 20), (long long)(budget.avail_floor_kb >> 10),
             (long long)budget.map_limit, budget.pid_bytes || budget.total_bytes ? "" : " (off)");
}

static int64_t budget_maps_read(void)
{
    int fd = open("/proc/self/maps", O_RDONLY | O_CLOEXEC);
    if (fd < 0) return 0;
    char buf[8192];
    int64_t n = 0;
    ssize_t k;
    while ((k = read(fd, buf, sizeof(buf))) > 0)
        for (ssize_t i = 0; i < k; i++)
            if (buf[i] == '\n') n++;
    close(fd);
    return n;
}

/* The maps line count, read at most once a second (a stats line and a big allocation each ask). */
int64_t vkb_mem_budget_maps(void)
{
    pthread_once(&budget_once, budget_init);
    int64_t now = (int64_t)time(NULL);
    if (__atomic_load_n(&budget.maps_at, __ATOMIC_RELAXED) != now) {
        __atomic_store_n(&budget.maps, budget_maps_read(), __ATOMIC_RELAXED);
        __atomic_store_n(&budget.maps_at, now, __ATOMIC_RELAXED);
    }
    return __atomic_load_n(&budget.maps, __ATOMIC_RELAXED);
}

static uint64_t budget_avail(void)
{
    pthread_once(&budget_once, budget_init);
    int64_t now = (int64_t)time(NULL);
    if (__atomic_load_n(&budget.avail_at, __ATOMIC_RELAXED) != now) {
        __atomic_store_n(&budget.avail_kb, vkb_memavail_kb(), __ATOMIC_RELAXED);
        __atomic_store_n(&budget.avail_at, now, __ATOMIC_RELAXED);
    }
    return __atomic_load_n(&budget.avail_kb, __ATOMIC_RELAXED);
}

static int64_t budget_pid_used_locked(int pid)
{
    int64_t v = 0;
    for (int i = 0; i < ST_PIDS; i++) {
        if (st.pid[i].pid == pid) v += st.pid[i].bytes;
        if (budget.res[i].pid == pid) v += budget.res[i].bytes;
    }
    return v;
}

static void budget_res_add_locked(int pid, int64_t d)
{
    int free_slot = -1;
    for (int i = 0; i < ST_PIDS; i++) {
        if (budget.res[i].pid == pid) {
            budget.res[i].bytes += d;
            if (budget.res[i].bytes <= 0) budget.res[i].pid = 0, budget.res[i].bytes = 0;
            return;
        }
        if (free_slot < 0 && (!budget.res[i].pid || budget.res[i].bytes <= 0)) free_slot = i;
    }
    if (free_slot >= 0 && d > 0) {
        budget.res[free_slot].pid = pid;
        budget.res[free_slot].bytes = d;
    }
}

static int64_t budget_live_locked(void)
{
    int64_t v = 0;
    for (int i = 0; i < CLS_N; i++) v += st.cls[i].bytes;
    return v + st.img_bc.bytes; /* decoded BC images are the driver's own allocations */
}

static void budget_deny(const char *what, int pid, uint64_t bytes, const char *why, int64_t used_pid, int64_t used_total)
{
    int n = __atomic_fetch_add(&budget_denials, 1, __ATOMIC_RELAXED);
    if (n >= 32) {
        if (n == 32) VKB_WARN("memory budget: further refusals are not logged");
        return;
    }
    char usage[160] = "";
    if (used_pid >= 0 && used_total >= 0)
        snprintf(usage, sizeof(usage), ", this client %lld MB of %lld, all clients %lld MB of %lld", (long long)(used_pid >> 20),
                 (long long)(budget.pid_bytes >> 20), (long long)(used_total >> 20), (long long)(budget.total_bytes >> 20));
    VKB_WARN("memory budget: %s for pid %d: %llu bytes (%llu MB) refused (%s)%s; MemAvailable %llu MB, maps %lld; "
             "VK_ERROR_OUT_OF_DEVICE_MEMORY", what, pid, (unsigned long long)bytes, (unsigned long long)(bytes >> 20), why, usage,
             (unsigned long long)(budget_avail() >> 10), (long long)vkb_mem_budget_maps());
}

static int budget_admit(int pid, uint64_t bytes, const char *what, int reserve)
{
    if (!bytes) return 0;
    pthread_once(&budget_once, budget_init);
    if (bytes > (uint64_t)INT64_MAX) {
        budget_deny(what, pid, bytes, "larger than the budget can count", -1, -1);
        return -1;
    }
    if (bytes >= BUDGET_BIG) {
        uint64_t avail = budget_avail();
        if (budget.avail_floor_kb > 0 && avail && avail < (uint64_t)budget.avail_floor_kb) {
            budget_deny(what, pid, bytes, "MemAvailable is under the floor", -1, -1);
            return -1;
        }
        int64_t maps = vkb_mem_budget_maps();
        if (budget.map_limit > 0 && maps > budget.map_limit) {
            budget_deny(what, pid, bytes, "too many mappings", -1, -1);
            return -1;
        }
    }
    pthread_mutex_lock(&st.lock);
    int64_t used_pid = budget_pid_used_locked(pid);
    int64_t used_total = budget_live_locked() + budget.reserved;
    uint64_t pid_used = used_pid > 0 ? (uint64_t)used_pid : 0;
    uint64_t total_used = used_total > 0 ? (uint64_t)used_total : 0;
    int pid_ok = !budget.pid_bytes || (pid_used <= (uint64_t)budget.pid_bytes && bytes <= (uint64_t)budget.pid_bytes - pid_used);
    int total_ok = !budget.total_bytes || (total_used <= (uint64_t)budget.total_bytes && bytes <= (uint64_t)budget.total_bytes - total_used);
    int ok = pid_ok && total_ok;
    if (ok && reserve) {
        budget_res_add_locked(pid, (int64_t)bytes);
        budget.reserved += (int64_t)bytes;
    }
    pthread_mutex_unlock(&st.lock);
    if (!ok) {
        budget_deny(what, pid, bytes, pid_ok ? "over the server's total" : "over this client's limit", used_pid, used_total);
        return -1;
    }
    return 0;
}

static void budget_res_release(int pid, uint64_t bytes)
{
    if (!bytes || bytes > (uint64_t)INT64_MAX) return;
    pthread_mutex_lock(&st.lock);
    budget_res_add_locked(pid, -(int64_t)bytes);
    budget.reserved -= (int64_t)bytes;
    if (budget.reserved < 0) budget.reserved = 0;
    pthread_mutex_unlock(&st.lock);
}

int vkb_mem_budget_reserve(int pid, uint64_t bytes) { return budget_admit(pid, bytes, "allocation", 1); }
void vkb_mem_budget_commit(int pid, uint64_t bytes) { budget_res_release(pid, bytes); }
void vkb_mem_budget_cancel(int pid, uint64_t bytes) { budget_res_release(pid, bytes); }

int vkb_mem_budget_device(int pid)
{
    pthread_once(&budget_once, budget_init);
    uint64_t avail = budget_avail();
    if (budget.avail_floor_kb > 0 && avail && avail < (uint64_t)budget.avail_floor_kb) {
        budget_deny("vkCreateDevice", pid, 0, "MemAvailable is under the floor", -1, -1);
        return -1;
    }
    int64_t maps = vkb_mem_budget_maps();
    if (budget.map_limit > 0 && maps > budget.map_limit) {
        budget_deny("vkCreateDevice", pid, 0, "too many mappings", -1, -1);
        return -1;
    }
    pthread_mutex_lock(&st.lock);
    int64_t used_pid = budget_pid_used_locked(pid);
    int64_t used_total = budget_live_locked() + budget.reserved;
    int ok = (!budget.pid_bytes || used_pid < budget.pid_bytes) && (!budget.total_bytes || used_total < budget.total_bytes);
    pthread_mutex_unlock(&st.lock);
    if (!ok) {
        budget_deny("vkCreateDevice", pid, 0, "already at a limit", used_pid, used_total);
        return -1;
    }
    return 0;
}

/* A rough size of an image for the threshold: 8 bytes a pixel (texels and, for BC, their decoded
 * form), all levels and layers. Only used to decide whether a create is "big", never accounted. */
VkDeviceSize vkb_mem_image_estimate(const VkImageCreateInfo *ci)
{
    if (!ci) return 0;
    uint64_t w = ci->extent.width, h = ci->extent.height, d = ci->extent.depth ? ci->extent.depth : 1;
    uint64_t l = ci->arrayLayers ? ci->arrayLayers : 1, m = ci->mipLevels ? ci->mipLevels : 1;
    if (!w || !h) return 0;
    uint64_t px = w * h;
    if (px / w != h) return UINT64_MAX;
    if (px > UINT64_MAX / d) return UINT64_MAX;
    px *= d;
    if (px > UINT64_MAX / l) return UINT64_MAX;
    px *= l;
    if (px > UINT64_MAX / m) return UINT64_MAX;
    px *= m;
    if (px > UINT64_MAX / 8) return UINT64_MAX;
    return (VkDeviceSize)(px * 8);
}

int vkb_mem_image_budget(int pid, const VkImageCreateInfo *ci)
{
    VkDeviceSize est = vkb_mem_image_estimate(ci);
    if (est < BUDGET_BIG) return 0;
    return budget_admit(pid, (uint64_t)est, "image", 0);
}

int vkb_mem_image_budget_current(const VkImageCreateInfo *ci)
{
    vkb_srv_table *t = vkb_srv_current_table();
    return vkb_mem_image_budget(t && t->proc ? t->proc->pid : 0, ci);
}

void vkb_mem_budget_start(void) { pthread_once(&budget_once, budget_init); }
'''


def apply_memory(data):
    replacements = [
        (
            b"    pthread_mutex_unlock(&st.lock);\n}\n\nuint8_t vkb_memstat_dedicated(const VkMemoryAllocateInfo *ai)",
            b"    pthread_mutex_unlock(&st.lock);\n}\n" + BUDGET_C.encode() + b"\nuint8_t vkb_memstat_dedicated(const VkMemoryAllocateInfo *ai)",
        ),
        (
            b"        int nfd = 0;\n"
            b"        DIR *dd = opendir(\"/proc/self/fd\");\n"
            b"        if (dd) {\n"
            b"            while (readdir(dd)) nfd++;\n"
            b"            closedir(dd);\n"
            b"        }\n"
            b"        uint64_t made = 0, pc_bytes = 0, pools = vkb_pools_overflow_live();",
            b"        int nfd = 0;\n"
            b"        DIR *dd = opendir(\"/proc/self/fd\");\n"
            b"        if (dd) {\n"
            b"            while (readdir(dd)) nfd++;\n"
            b"            closedir(dd);\n"
            b"        }\n"
            b"        int maps = (int)vkb_mem_budget_maps();\n"
            b"        uint64_t made = 0, pc_bytes = 0, pools = vkb_pools_overflow_live();",
        ),
        (
            b"        ADD(\"mem: rss %ldM (anon %ldM file %ldM shmem %ldM) heap %lldM fds %d | free %lluM swap %lluM%s | live %lldM/%lld:\", rss >> 10,\n"
            b"            anon >> 10, file >> 10, shm >> 10, heap, nfd - 2, (unsigned long long)(avail >> 10), (unsigned long long)(swap >> 10),\n"
            b"            low ? \" (low)\" : \"\", MB(live), (long long)nlive);",
            b"        ADD(\"mem: rss %ldM (anon %ldM file %ldM shmem %ldM) heap %lldM fds %d | free %lluM (MemAvailable) swap %lluM%s | maps %d | live %lldM/%lld:\", rss >> 10,\n"
            b"            anon >> 10, file >> 10, shm >> 10, heap, nfd - 2, (unsigned long long)(avail >> 10),\n"
            b"            (unsigned long long)(swap >> 10), low ? \" (low)\" : \"\", maps, MB(live), (long long)nlive);",
        ),
        (
            b"        if (lines) VKB_INFO(\"%s | bc scratch %lldM\", line, MB(vkb_emu_bcn_scratch_live()));\n"
            b"        if (entered && ++lows <= 4)",
            b"        if (lines) VKB_INFO(\"%s | bc scratch %lldM\", line, MB(vkb_emu_bcn_scratch_live()));\n"
            b"        if (budget.map_limit > 0 && maps > budget.map_limit) {\n"
            b"            static int told;\n"
            b"            if (__atomic_fetch_add(&told, 1, __ATOMIC_RELAXED) < 4)\n"
            b"                VKB_WARN(\"maps: %d mappings, over the %lld limit; new devices and allocations of 64 MB or more are refused\",\n"
            b"                         maps, (long long)budget.map_limit);\n"
            b"        }\n"
            b"        if (entered && ++lows <= 4)",
        ),
        (
            b"void vkb_mem_free_all(vkb_srv_table *dev)\n"
            b"{\n"
            b"    for (int i = 0; i < 256; i++) {\n"
            b"        vkb_srv_mem *m = dev->mems[i];\n"
            b"        dev->mems[i] = NULL;\n"
            b"        while (m) {\n"
            b"            vkb_srv_mem *n = m->next;\n"
            b"            vkb_mem_release(dev, m);\n"
            b"            m = n;\n"
            b"        }\n"
            b"    }\n"
            b"}",
            b"void vkb_mem_free_all(vkb_srv_table *dev)\n"
            b"{\n"
            b"    int64_t n = 0, bytes = 0;\n"
            b"    for (int i = 0; i < 256; i++) {\n"
            b"        vkb_srv_mem *m = dev->mems[i];\n"
            b"        dev->mems[i] = NULL;\n"
            b"        while (m) {\n"
            b"            vkb_srv_mem *next = m->next;\n"
            b"            n++, bytes += (int64_t)m->size;\n"
            b"            vkb_mem_release(dev, m);\n"
            b"            m = next;\n"
            b"        }\n"
            b"    }\n"
            b"    if (n) VKB_INFO(\"device %u (pid %d): released %lld allocation(s), %lld MB of GPU memory\", dev->id,\n"
            b"                    dev->proc ? dev->proc->pid : 0, (long long)n, MB(bytes));\n"
            b"}",
        ),
        (
            b"    uint32_t asked_type = ai.memoryTypeIndex;\n"
            b"    uint8_t ded = vkb_memstat_dedicated(&ai); /* before the strategies drop the struct from the chain */\n"
            b"    if (mode == VKB_ALLOC_SHARED || mode == VKB_ALLOC_SHARED_CACHED) {",
            b"    uint32_t asked_type = ai.memoryTypeIndex;\n"
            b"    uint8_t ded = vkb_memstat_dedicated(&ai); /* before the strategies drop the struct from the chain */\n"
            b"    int budget_pid = t->proc ? t->proc->pid : 0;\n"
            b"    if (vkb_mem_budget_reserve(budget_pid, (uint64_t)asked) != 0) {\n"
            b"        if (client_fd >= 0) close(client_fd);\n"
            b"        log_alloc(t, &ai, asked_type, ded, \"refused by the memory budget\", VK_ERROR_OUT_OF_DEVICE_MEMORY);\n"
            b"        vkb_enc_u32(&c->r, (uint32_t)VK_ERROR_OUT_OF_DEVICE_MEMORY);\n"
            b"        vkb_enc_u64(&c->r, 0);\n"
            b"        vkb_enc_fd(&c->r, -1);\n"
            b"        vkb_enc_u64(&c->r, 0);\n"
            b"        return;\n"
            b"    }\n"
            b"    int budget_held = 1;\n"
            b"    if (mode == VKB_ALLOC_SHARED || mode == VKB_ALLOC_SHARED_CACHED) {",
        ),
        (
            b"allocated:\n"
            b"    if (m) {\n"
            b"        m->stat_ded = ded;\n"
            b"        vkb_mem_track(t, m);\n"
            b"    }\n"
            b"    alloc_timed(t, asked, asked_type, ded, t0, r);",
            b"allocated:\n"
            b"    if (m) {\n"
            b"        m->stat_ded = ded;\n"
            b"        vkb_mem_track(t, m);\n"
            b"    }\n"
            b"    if (budget_held) {\n"
            b"        if (m) vkb_mem_budget_commit(budget_pid, (uint64_t)asked);\n"
            b"        else vkb_mem_budget_cancel(budget_pid, (uint64_t)asked);\n"
            b"        budget_held = 0;\n"
            b"    }\n"
            b"    alloc_timed(t, asked, asked_type, ded, t0, r);",
        ),
    ]
    for old, new in replacements:
        if data.count(old) != 1:
            raise SystemExit("server_memory.c anchor missing or duplicated: " + repr(old[:90]))
        data = data.replace(old, new)
    return data


def apply_instance(data):
    replacements = [
        (
            b"    VkDevice dev = VK_NULL_HANDLE;\n"
            b"    VkResult r = inst->real.vkCreateDevice(pd, ci, NULL, &dev);\n"
            b"    uint32_t id = 0;\n"
            b"    if (r == VK_SUCCESS) {\n"
            b"        vkb_srv_table *t = calloc(1, sizeof(*t));\n"
            b"        pthread_mutex_init(&t->lock, NULL);",
            b"    VkDevice dev = VK_NULL_HANDLE;\n"
            b"    VkResult r;\n"
            b"    {\n"
            b"        int pid = inst->proc ? inst->proc->pid : 0;\n"
            b"        if (vkb_mem_budget_device(pid) != 0) r = VK_ERROR_OUT_OF_DEVICE_MEMORY;\n"
            b"        else r = inst->real.vkCreateDevice(pd, ci, NULL, &dev);\n"
            b"    }\n"
            b"    uint32_t id = 0;\n"
            b"    if (r == VK_SUCCESS) {\n"
            b"        vkb_srv_table *t = calloc(1, sizeof(*t));\n"
            b"        if (!t) {\n"
            b"            VKB_ERR(\"vkCreateDevice: no table for the new device; destroying it\");\n"
            b"            inst->real.vkDestroyDevice(dev, NULL);\n"
            b"            dev = VK_NULL_HANDLE;\n"
            b"            r = VK_ERROR_OUT_OF_HOST_MEMORY;\n"
            b"        } else {\n"
            b"        pthread_mutex_init(&t->lock, NULL);",
        ),
        (
            b"            vkb_mem_log_types(t, id);\n"
            b"            for (uint32_t i = 0; i < n; i++) VKB_DBG(\"  device extension %s\", exts[i]);\n"
            b"        }\n"
            b"    } else {\n"
            b"        VKB_ERR(\"vkCreateDevice failed: %d (%u extensions)\", r, n);",
            b"            vkb_mem_log_types(t, id);\n"
            b"            for (uint32_t i = 0; i < n; i++) VKB_DBG(\"  device extension %s\", exts[i]);\n"
            b"        }\n"
            b"        }\n"
            b"    } else {\n"
            b"        if (dev) {\n"
            b"            VKB_ERR(\"vkCreateDevice failed (%d) but left a device handle; destroying it\", r);\n"
            b"            inst->real.vkDestroyDevice(dev, NULL);\n"
            b"            dev = VK_NULL_HANDLE;\n"
            b"        }\n"
            b"        VKB_ERR(\"vkCreateDevice failed: %d (%u extensions); no table registered, later device calls are refused\", r, n);",
        ),
    ]
    for old, new in replacements:
        if data.count(old) != 1:
            raise SystemExit("server_instance.c anchor missing or duplicated: " + repr(old[:90]))
        data = data.replace(old, new)
    return data


def apply_main(data):
    replacements = [
        (
            b"    fprintf(stderr, \"usage: vkbridge-server --socket PATH [--log FILE] [--vulkan LIB] [--cache-dir DIR] [--selftest] [--verbose]\\n\");",
            b"    fprintf(stderr, \"usage: vkbridge-server --socket PATH [--log FILE] [--vulkan LIB] [--cache-dir DIR] [--selftest] [--verbose]\\n\"\n"
            b"                    \"       [--mem-budget-pid-mb N] [--mem-budget-total-mb N] [--mem-min-avail-mb N] [--map-limit N]\\n\");",
        ),
        (
            b"        else if (!strcmp(argv[i], \"--selftest\")) selftest_only = 1;\n"
            b"        else if (!strcmp(argv[i], \"--verbose\")) vkb_verbose = 1;",
            b"        else if (!strcmp(argv[i], \"--selftest\")) selftest_only = 1;\n"
            b"        else if (!strcmp(argv[i], \"--mem-budget-pid-mb\") && i + 1 < argc) setenv(\"VKBRIDGE_MEM_BUDGET_PID_MB\", argv[++i], 1);\n"
            b"        else if (!strcmp(argv[i], \"--mem-budget-total-mb\") && i + 1 < argc) setenv(\"VKBRIDGE_MEM_BUDGET_TOTAL_MB\", argv[++i], 1);\n"
            b"        else if (!strcmp(argv[i], \"--mem-min-avail-mb\") && i + 1 < argc) setenv(\"VKBRIDGE_MEM_MIN_AVAIL_MB\", argv[++i], 1);\n"
            b"        else if (!strcmp(argv[i], \"--map-limit\") && i + 1 < argc) setenv(\"VKBRIDGE_MAP_LIMIT\", argv[++i], 1);\n"
            b"        else if (!strcmp(argv[i], \"--verbose\")) vkb_verbose = 1;",
        ),
        (
            b"    vkb_srv_env_reload(); /* <cache dir>/env: VKBRIDGE_FAKE_MISSING and the like reach the self-test */",
            b"    vkb_srv_env_reload(); /* <cache dir>/env: VKBRIDGE_FAKE_MISSING and the like reach the self-test */\n"
            b"    vkb_mem_budget_start(); /* thresholds from the environment, said in the log before any client */",
        ),
    ]
    for old, new in replacements:
        if data.count(old) != 1:
            raise SystemExit("server_main.c anchor missing or duplicated: " + repr(old[:90]))
        data = data.replace(old, new)
    return data


def apply_header(data):
    old = b"void vkb_mem_free_all(vkb_srv_table *dev);"
    new = (
        b"void vkb_mem_free_all(vkb_srv_table *dev);\n"
        b"/* Memory budget (server_memory.c): checked before a client's allocation, a new device or a big\n"
        b" * image reaches the driver; over a limit VK_ERROR_OUT_OF_DEVICE_MEMORY, never an abort. Limits:\n"
        b" * VKBRIDGE_MEM_BUDGET_PID_MB (1024), _TOTAL_MB (2048), VKBRIDGE_MEM_MIN_AVAIL_MB (512),\n"
        b" * VKBRIDGE_MAP_LIMIT (50000). reserve/commit/cancel bracket an allocation; image/device check\n"
        b" * without reserving. */\n"
        b"void vkb_mem_budget_start(void);\n"
        b"int vkb_mem_budget_reserve(int pid, uint64_t bytes);\n"
        b"void vkb_mem_budget_commit(int pid, uint64_t bytes);\n"
        b"void vkb_mem_budget_cancel(int pid, uint64_t bytes);\n"
        b"int vkb_mem_budget_device(int pid);\n"
        b"int vkb_mem_image_budget(int pid, const VkImageCreateInfo *ci);\n"
        b"int vkb_mem_image_budget_current(const VkImageCreateInfo *ci); /* the connection thread's client */\n"
        b"VkDeviceSize vkb_mem_image_estimate(const VkImageCreateInfo *ci);\n"
        b"int64_t vkb_mem_budget_maps(void);"
    )
    if data.count(old) != 1:
        raise SystemExit("vkb_server.h anchor missing or duplicated")
    return data.replace(old, new)


def apply_gen(data):
    replacements = [
        (
            b"#include \"../vkb_server_iface.h\"\n#include <string.h>",
            b"#include \"../vkb_server_iface.h\"\n#include <string.h>\n\n"
            b"/* server_memory.c: the memory budget's check before a big image is created (the allocation\n"
            b" * that binds it is checked with the client's other allocations). */\n"
            b"extern int vkb_mem_image_budget_current(const VkImageCreateInfo *ci);",
        ),
        (
            b"    if (d->err) { vkb_srv_bad_message(c); return; }\n"
            b"    if (!c->dt->vkCreateImage) { vkb_srv_missing(c, \"vkCreateImage\"); return; }\n"
            b"    VkResult r_ = c->dt->vkCreateImage(device, pCreateInfo, NULL, pImage);",
            b"    if (d->err) { vkb_srv_bad_message(c); return; }\n"
            b"    if (!c->dt->vkCreateImage) { vkb_srv_missing(c, \"vkCreateImage\"); return; }\n"
            b"    if (pCreateInfo && vkb_mem_image_budget_current(pCreateInfo) != 0) {\n"
            b"        VkResult budget_r_ = VK_ERROR_OUT_OF_DEVICE_MEMORY;\n"
            b"        VkImage budget_none_ = VK_NULL_HANDLE;\n"
            b"        vkb_enc_bytes(e, &budget_r_, sizeof(budget_r_));\n"
            b"        vkb_enc_u8(e, pImage != NULL);\n"
            b"        if (pImage) vkb_enc_raw(e, &budget_none_, sizeof(budget_none_));\n"
            b"        return;\n"
            b"    }\n"
            b"    VkResult r_ = c->dt->vkCreateImage(device, pCreateInfo, NULL, pImage);",
        ),
    ]
    for old, new in replacements:
        if data.count(old) != 1:
            raise SystemExit("vkb_gen_server.c anchor missing or duplicated: " + repr(old[:90]))
        data = data.replace(old, new)
    return data


APPLY = {
    "server/server_memory.c": apply_memory,
    "server/server_instance.c": apply_instance,
    "server/server_main.c": apply_main,
    "server/vkb_server.h": apply_header,
    "common/gen/vkb_gen_server.c": apply_gen,
}


def candidate(data, name):
    newline = b"\r\n" if b"\r\n" in data else b"\n"
    data = canonical(data)
    data = APPLY[name](data)
    return data.replace(b"\n", newline)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("source", type=Path)
    ap.add_argument("--check", action="store_true", help="Validate pristine baseline without writing")
    ap.add_argument("--verify-candidate", action="store_true", help="Validate exactly the expected candidate without writing")
    args = ap.parse_args()
    if args.check and args.verify_candidate:
        ap.error("Select at most one check mode")
    root = args.source.resolve()
    if git(root, "rev-parse", "HEAD") != REVISION:
        raise SystemExit("Refusing source revision other than " + REVISION)
    for name, expected in BLOBS.items():
        recorded = git(root, "rev-parse", "HEAD:" + name)
        if recorded != expected:
            raise SystemExit("Unexpected committed blob: " + name)
        data = (root / name).read_bytes()
        if args.verify_candidate:
            pristine = subprocess.check_output(["git", "-C", str(root), "show", "HEAD:" + name])
            if canonical(data) != canonical(candidate(pristine, name)):
                raise SystemExit("Candidate content mismatch: " + name)
        elif blob_hash(data) != expected:
            raise SystemExit("Working-tree blob differs from baseline: " + name)
        print(name, "git-blob=" + recorded, "sha256=" + hashlib.sha256(data).hexdigest())
    changes = sorted(git(root, "diff", "--name-only", "HEAD").splitlines())
    allowed = sorted(BLOBS) if args.verify_candidate else []
    if changes != allowed:
        raise SystemExit("Unexpected tracked changes: " + repr(changes))
    if not args.check and not args.verify_candidate:
        for name in BLOBS:
            path = root / name
            path.write_bytes(candidate(path.read_bytes(), name))
        print("Applied server memory budget candidate: per-client 1024 MB, total 2048 MB, MemAvailable floor 512 MB, map limit 50000")
    else:
        print("Verified", "candidate" if args.verify_candidate else "pristine baseline")


if __name__ == "__main__":
    main()
