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
 * libwayland-client does not export wl_surface_destroy (a static inline in
 * wayland-client-protocol.h); its caller goes through the exported wl_proxy_marshal_flags()
 * (opcode 0 = WL_SURFACE_DESTROY, flags bit 0 = WL_MARSHAL_FLAG_DESTROY). That is the interposition
 * point. wl_proxy_marshal_flags is variadic and cannot be forwarded from C without copying the
 * argument area, so the symbol is an aarch64 trampoline: it saves the argument registers, calls
 * vkb_wl_pre_destroy() for a destroy request, restores them and branches to the real function with
 * the caller's stack arguments untouched. (Interposing wl_proxy_marshal_array_flags instead only
 * works when the library calls it through the GOT; Ubuntu's libwayland 1.24 calls it directly.)
 *
 * The Vulkan loader opens the ICD with RTLD_LAZY|RTLD_LOCAL, so dlsym(RTLD_DEFAULT) cannot see it:
 * the shim finds the loaded object by path in /proc/self/maps and dlsym()s that handle, with
 * RTLD_NOLOAD so a second copy is never loaded. Without the hook (another ICD, another driver) the
 * shim is a pass-through: only a destroy request takes the extra check.
 */
#define _GNU_SOURCE
#include <dlfcn.h>
#include <pthread.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

struct wl_proxy;
struct wl_interface;

#define WL_SURFACE_DESTROY 0u
#define WL_MARSHAL_FLAG_DESTROY 1u /* wayland-client-core.h: (1 << 0) */

typedef void (*gone_fn)(struct wl_proxy *);
typedef const struct wl_interface *(*iface_fn)(struct wl_proxy *);

/* Read by the trampoline. Hidden: the shim's only exported symbol is wl_proxy_marshal_flags. */
__attribute__((visibility("hidden"))) void *vkb_wl_real_marshal_flags;

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

/* Our imports, and the ICD's hook if it is loaded. Retried a few times, because a surface can be
 * destroyed before the Vulkan loader has finished opening the ICD. */
__attribute__((visibility("hidden"))) void vkb_wl_resolve(void)
{
    pthread_mutex_lock(&resolve_mu);
    if (!vkb_wl_real_marshal_flags) vkb_wl_real_marshal_flags = dlsym(RTLD_NEXT, "wl_proxy_marshal_flags");
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

/* From the trampoline: a destroy request on any proxy. Only a wl_surface reaches the hook. */
__attribute__((visibility("hidden"))) void vkb_wl_pre_destroy(struct wl_proxy *proxy)
{
    if (!proxy_iface || !surface_iface) vkb_wl_resolve();
    if (!proxy || !proxy_iface || !surface_iface) return;
    if (proxy_iface(proxy) != surface_iface) return;
    __atomic_fetch_add(&surfaces, 1, __ATOMIC_RELAXED);
    if (!gone) vkb_wl_resolve(); /* the ICD may have been opened since */
    if (gone) {
        __atomic_fetch_add(&hooked, 1, __ATOMIC_RELAXED);
        if (trace_on()) fprintf(stderr, "vkb-wlhook: wl_surface destroy -> vkb_wayland_surface_gone\n");
        gone(proxy); /* marks the surface and drains a present naming it; queues nothing */
    } else {
        __atomic_fetch_add(&misses, 1, __ATOMIC_RELAXED);
    }
}

static void summary(void)
{
    if (!trace_on()) return;
    fprintf(stderr, "vkb-wlhook: wl_surface destroys %lu, hooked %lu, misses %lu\n",
            __atomic_load_n(&surfaces, __ATOMIC_RELAXED), __atomic_load_n(&hooked, __ATOMIC_RELAXED),
            __atomic_load_n(&misses, __ATOMIC_RELAXED));
}

__attribute__((constructor)) static void vkb_wl_init(void)
{
    vkb_wl_resolve();
    atexit(summary);
}

/*
 * struct wl_proxy *wl_proxy_marshal_flags(struct wl_proxy *proxy, uint32_t opcode,
 *                                         const struct wl_interface *iface, uint32_t version,
 *                                         uint32_t flags, ...);
 * x0 proxy, w1 opcode, x2 iface, w3 version, w4 flags; the variadic arguments follow in x5-x7 and
 * on the stack. Saving/restoring x0-x9 leaves every one of them as the caller passed it, and the
 * stack frame is popped before the branch, so the real function sees the original stack arguments.
 */
#if defined(__aarch64__)
__asm__(
    ".text\n"
    ".global wl_proxy_marshal_flags\n"
    ".type wl_proxy_marshal_flags, %function\n"
    "wl_proxy_marshal_flags:\n"
    "    stp x29, x30, [sp, #-16]!\n"
    "    stp x0, x1, [sp, #-16]!\n"
    "    stp x2, x3, [sp, #-16]!\n"
    "    stp x4, x5, [sp, #-16]!\n"
    "    stp x6, x7, [sp, #-16]!\n"
    "    stp x8, x9, [sp, #-16]!\n"
    "    cbnz w1, 1f\n"
    "    tbz w4, #0, 1f\n"
    "    bl vkb_wl_pre_destroy\n"
    "1:\n"
    "    ldp x8, x9, [sp], #16\n"
    "    ldp x6, x7, [sp], #16\n"
    "    ldp x4, x5, [sp], #16\n"
    "    ldp x2, x3, [sp], #16\n"
    "    ldp x0, x1, [sp], #16\n"
    "    ldp x29, x30, [sp], #16\n"
    "    adrp x16, vkb_wl_real_marshal_flags\n"
    "    ldr x16, [x16, #:lo12:vkb_wl_real_marshal_flags]\n"
    "    cbnz x16, 2f\n"
    "    bl vkb_wl_resolve\n"
    "    adrp x16, vkb_wl_real_marshal_flags\n"
    "    ldr x16, [x16, #:lo12:vkb_wl_real_marshal_flags]\n"
    "2:\n"
    "    br x16\n"
);
#else
#error "the wl-gone shim interposes wl_proxy_marshal_flags with an aarch64 trampoline"
#endif
