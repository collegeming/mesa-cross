/* SPDX-License-Identifier: GPL-3.0-or-later
 * Copyright (C) 2026 ASEbrahim and MaliBridge contributors
 *
 * LD_PRELOAD shim: tell the MaliBridge ICD that a wl_surface is being destroyed before libwayland
 * queues wl_surface.destroy.
 *
 * Noctalia (and any EGL/GLES app on Mesa's zink/Kopper) destroys its wl_surface while Kopper still
 * holds the VkSurfaceKHR built on it: Kopper's displaytarget cache is keyed by the wl_surface
 * pointer and its batch keeps the displaytarget referenced, so the VkSurface is destroyed late and
 * a present can attach to a wl_surface niri has already forgotten ("invalid object N", the whole
 * connection ends). MaliBridge cannot see that destroy from the Vulkan side, so it exports
 * vkb_wayland_surface_gone(); this shim calls it from inside libwayland's own destroy path, while
 * the proxy is still valid and before the destroy request is queued.
 *
 * libwayland-client does not export wl_surface_destroy (it is a static inline in
 * wayland-client-protocol.h). wl_proxy_marshal_flags() calls the exported
 * wl_proxy_marshal_array_flags() through the PLT/GOT, so that is the interposition point:
 * WL_SURFACE_DESTROY is opcode 0 and its caller passes WL_MARSHAL_FLAG_DESTROY (1 << 0).
 *
 * The Vulkan loader opens the ICD with RTLD_LAZY|RTLD_LOCAL, so dlsym(RTLD_DEFAULT) cannot see it:
 * the shim finds the loaded object by path in /proc/self/maps and dlsym()s that handle, with
 * RTLD_NOLOAD so a second copy is never loaded. Without the hook (another ICD, another driver) the
 * shim is a pass-through: one extra early return per request.
 */
#define _GNU_SOURCE
#include <dlfcn.h>
#include <pthread.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

struct wl_proxy;
struct wl_interface;
union wl_argument;

#define WL_SURFACE_DESTROY 0u
#define WL_MARSHAL_FLAG_DESTROY 1u /* wayland-client-core.h: (1 << 0) */

typedef struct wl_proxy *(*marshal_fn)(struct wl_proxy *, uint32_t, const struct wl_interface *, uint32_t, uint32_t,
                                       const union wl_argument *);
typedef void (*gone_fn)(struct wl_proxy *);
typedef const struct wl_interface *(*iface_fn)(struct wl_proxy *);

static marshal_fn real_marshal;
static gone_fn gone;
static iface_fn proxy_iface;
static const struct wl_interface *surface_iface;

static pthread_mutex_t resolve_mu = PTHREAD_MUTEX_INITIALIZER;
static int resolve_tries;

static unsigned long surfaces, hooked, misses; /* atomics: any thread may destroy */

static int trace_on(void)
{
    static int v = -1;
    if (v < 0) {
        const char *e = getenv("VKBRIDGE_WLHOOK_TRACE");
        if (!e || !*e) e = getenv("VKBRIDGE_WSI_TRACE");
        v = e && *e && strcmp(e, "0") != 0;
    }
    return v;
}

/* Once: our own imports, and the ICD's hook if it is loaded. Retried a few times, because a
 * surface can be destroyed before the Vulkan loader has finished opening the ICD. */
static void resolve(void)
{
    pthread_mutex_lock(&resolve_mu);
    if (!real_marshal) real_marshal = (marshal_fn)dlsym(RTLD_NEXT, "wl_proxy_marshal_array_flags");
    if (!proxy_iface) proxy_iface = (iface_fn)dlsym(RTLD_DEFAULT, "wl_proxy_get_interface");
    if (!surface_iface) surface_iface = (const struct wl_interface *)dlsym(RTLD_DEFAULT, "wl_surface_interface");
    if (!gone && resolve_tries < 8) {
        char path[4096] = {0};
        const char *env = getenv("VKB_WAYLAND_HOOK_LIB");
        resolve_tries++;
        if (env && *env) {
            snprintf(path, sizeof(path), "%s", env);
        } else {
            FILE *f = fopen("/proc/self/maps", "r");
            char line[4096];
            while (f && fgets(line, sizeof(line), f)) {
                char *p = strstr(line, "libvulkan_droidbridge.so");
                char *slash = strchr(line, '/');
                if (!p || !slash) continue;
                char *nl = strchr(slash, '\n');
                if (nl) *nl = 0;
                snprintf(path, sizeof(path), "%s", slash);
                break;
            }
            if (f) fclose(f);
        }
        if (path[0]) {
            void *h = dlopen(path, RTLD_NOW | RTLD_NOLOAD);
            if (h) gone = (gone_fn)dlsym(h, "vkb_wayland_surface_gone");
        }
        if (trace_on())
            fprintf(stderr, "vkb-wlhook: resolve try %d path=%s hook=%p\n", resolve_tries, path[0] ? path : "(none)",
                    (void *)gone);
    }
    pthread_mutex_unlock(&resolve_mu);
}

static void summary(void)
{
    if (!trace_on()) return;
    fprintf(stderr, "vkb-wlhook: wl_surface destroys %lu, hooked %lu, misses %lu\n",
            __atomic_load_n(&surfaces, __ATOMIC_RELAXED), __atomic_load_n(&hooked, __ATOMIC_RELAXED),
            __atomic_load_n(&misses, __ATOMIC_RELAXED));
}

__attribute__((constructor)) static void init(void)
{
    atexit(summary);
}

struct wl_proxy *wl_proxy_marshal_array_flags(struct wl_proxy *proxy, uint32_t opcode, const struct wl_interface *iface,
                                               uint32_t version, uint32_t flags, const union wl_argument *args)
{
    if (!real_marshal) resolve();
    if (!real_marshal) return NULL;

    if (proxy && opcode == WL_SURFACE_DESTROY && (flags & WL_MARSHAL_FLAG_DESTROY)) {
        int is_surface = proxy_iface && surface_iface && proxy_iface(proxy) == surface_iface;
        if (is_surface) {
            __atomic_fetch_add(&surfaces, 1, __ATOMIC_RELAXED);
            if (!gone) resolve(); /* the ICD may have been opened since */
            if (gone) {
                __atomic_fetch_add(&hooked, 1, __ATOMIC_RELAXED);
                if (trace_on()) fprintf(stderr, "vkb-wlhook: wl_surface destroy -> vkb_wayland_surface_gone\n");
                gone(proxy); /* marks the surface and drains a present naming it; queues nothing */
            } else {
                __atomic_fetch_add(&misses, 1, __ATOMIC_RELAXED);
            }
        }
    }
    return real_marshal(proxy, opcode, iface, version, flags, args);
}
