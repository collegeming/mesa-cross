#!/usr/bin/env python3
"""Apply/check the 47ec18e "wl_surface gone before its VkSurface" candidate (scheme A).

Applies on top of the format-capacity patch and the WSI lifecycle patch: only
client/wsi.h, client/wsi.c and client/wsi_wayland.c change. The ICD gains a
wl_surface -> vkb_surface registry and an exported vkb_wayland_surface_gone(),
called by the LD_PRELOAD shim before libwayland queues wl_surface.destroy.
"""
import argparse
import hashlib
import subprocess
from pathlib import Path

REVISION = "47ec18eaa9d9f83c359d56ba412a17d61ba59a66"
# The exact lifecycle candidate (capacity fix included), LF canonical.
PRE = {
    "client/wsi.h": "89fbafc3e4364ee066a6c9cc7f8a807a1ed560b892ca11953d250de2edaf2236",
    "client/wsi.c": "2b8154400f27965e4865020896e54009e050b84fbeb6394c5570d8a7ba97a0ed",
    "client/wsi_wayland.c": "c1bacef9bed28e9c9cdc166c2ee5a8eaad2456e79e153ea5f23fad38bfc28a05",
}
# The exact post-patch candidate (LF canonical), for --verify-candidate.
POST = {
    "client/wsi.h": "fc09877eb41d179c00eb633d2f38a6fbaf7008c5a8199d80bc2b7aa00b15d2ba",
    "client/wsi.c": "ce0bee15dc432bb917481023adcc6861b13350a915a8ac7aa422a8aba7d1f96a",
    "client/wsi_wayland.c": "bc19fcd6035514d4d3a5e251f513c38b5783be0ad3ee976f55e2f542961bd241",
}

WSI_H = [
    (b"    pthread_mutex_t lock;\n"
     b"    int refs;                /* the app's, and one per swapchain built on it */\n"
     b"    int dead;                /* no request may name its wl_surface any more */\n"
     b"    vkb_swapchain *chains;   /* the swapchains built on it (their `next`) */\n"
     b"} vkb_surface;",
     b"    pthread_mutex_t lock;\n"
     b"    int refs;                /* the app's, and one per swapchain built on it */\n"
     b"    int dead;                /* no request may name its wl_surface any more */\n"
     b"    /* The wl_surface usually goes before this VkSurface (Mesa's zink/Kopper releases its\n"
     b"     * displaytarget late): wsi.c's vkb_wayland_surface_gone(), called by the shim from\n"
     b"     * libwayland's destroy, takes this to drain a present that is naming the wl_surface. */\n"
     b"    pthread_mutex_t present_mu;\n"
     b"    struct vkb_surface *reg_next; /* the wl_surface registry (wsi.c) */\n"
     b"    vkb_swapchain *chains;   /* the swapchains built on it (their `next`) */\n"
     b"} vkb_surface;"),
    (b"    vkb_surface *surface;\n"
     b"    vkb_swapchain *next;      /* the surface's list of its swapchains */\n",
     b"    vkb_surface *surface;\n"
     b"    vkb_swapchain *next;      /* the surface's list of its swapchains */\n"
     b"    int surface_gone;         /* its wl_surface is being destroyed: no request may name it (atomic) */\n"),
    (b"/* Helpers in wsi.c */\n"
     b"/* 1 once the app destroyed the surface: presents on its swapchains then fail with\n"
     b" * VK_ERROR_SURFACE_LOST_KHR instead of naming a destroyed wl_surface. */\n"
     b"int vkb_surface_is_dead(vkb_surface *s);\n",
     b"/* Helpers in wsi.c */\n"
     b"/* 1 once the app destroyed the surface: presents on its swapchains then fail with\n"
     b" * VK_ERROR_SURFACE_LOST_KHR instead of naming a destroyed wl_surface. */\n"
     b"int vkb_surface_is_dead(vkb_surface *s);\n"
     b"/* The shim calls this before libwayland queues wl_surface.destroy (WL_MARSHAL_FLAG_DESTROY on\n"
     b" * wl_surface): the surface and its swapchains are marked, and a present in flight is drained\n"
     b" * (bounded). Only the marks: the VkSwapchainKHR and its wl_buffers stay the app's. */\n"
     b"VKB_EXPORT void vkb_wayland_surface_gone(struct wl_surface *surface);\n"),
]

WSI_C = [
    (b"    pthread_mutex_init(&s->lock, NULL);\n    s->refs = 1;\n",
     b"    pthread_mutex_init(&s->lock, NULL);\n    pthread_mutex_init(&s->present_mu, NULL);\n    s->refs = 1;\n"),
    (b"    if (!refs) {\n        pthread_mutex_destroy(&s->lock);\n        free(s);\n    }\n",
     b"    if (!refs) {\n        pthread_mutex_destroy(&s->present_mu);\n        pthread_mutex_destroy(&s->lock);\n        free(s);\n    }\n"),
    (b"int vkb_surface_is_dead(vkb_surface *s)\n"
     b"{\n"
     b"    if (!s) return 0;\n"
     b"    pthread_mutex_lock(&s->lock);\n"
     b"    int dead = s->dead;\n"
     b"    pthread_mutex_unlock(&s->lock);\n"
     b"    return dead;\n"
     b"}\n",
     b"int vkb_surface_is_dead(vkb_surface *s)\n"
     b"{\n"
     b"    if (!s) return 0;\n"
     b"    pthread_mutex_lock(&s->lock);\n"
     b"    int dead = s->dead;\n"
     b"    pthread_mutex_unlock(&s->lock);\n"
     b"    return dead;\n"
     b"}\n"
     b"\n"
     b"/* ------------------------------------------------------------------ the wl_surface registry */\n"
     b"\n"
     b"/*\n"
     b" * The app destroys its wl_surface through libwayland, while Mesa's zink/Kopper may still hold\n"
     b" * the VkSurfaceKHR it built on that wl_surface (the displaytarget cache is keyed by the\n"
     b" * wl_surface pointer and the batch keeps it referenced). The LD_PRELOAD shim calls\n"
     b" * vkb_wayland_surface_gone() from inside libwayland's own destroy, before the request is\n"
     b" * queued: the surface and its swapchains are marked, so no attach or commit follows the\n"
     b" * destroy (a compositor's \"invalid object\" ends the whole connection).\n"
     b" *\n"
     b" * wl_reg_lock covers the list; a surface leaves it in vkDestroySurfaceKHR and here. Ordering:\n"
     b" * wl_reg_lock -> s->lock -> sc->lock. Nothing takes s->lock while holding present_mu (a present\n"
     b" * only checks the atomic surface_gone there), so the hook's s->lock -> present_mu drain and the\n"
     b" * present path's present_mu -> (nothing) cannot deadlock.\n"
     b" */\n"
     b"static pthread_mutex_t wl_reg_lock = PTHREAD_MUTEX_INITIALIZER;\n"
     b"static vkb_surface *wl_reg;\n"
     b"\n"
     b"static int wl_gone_trace(void)\n"
     b"{\n"
     b"    static int v = -1;\n"
     b"    if (v < 0) {\n"
     b"        const char *e = getenv(\"VKBRIDGE_WSI_TRACE\");\n"
     b"        v = e && *e && strcmp(e, \"0\") != 0;\n"
     b"    }\n"
     b"    return v;\n"
     b"}\n"
     b"\n"
     b"static void wl_reg_add(vkb_surface *s)\n"
     b"{\n"
     b"    pthread_mutex_lock(&wl_reg_lock);\n"
     b"    s->reg_next = wl_reg;\n"
     b"    wl_reg = s;\n"
     b"    pthread_mutex_unlock(&wl_reg_lock);\n"
     b"}\n"
     b"\n"
     b"/* wl_reg_lock held. */\n"
     b"static void wl_reg_unlink_locked(vkb_surface *s)\n"
     b"{\n"
     b"    for (vkb_surface **p = &wl_reg; *p; p = &(*p)->reg_next)\n"
     b"        if (*p == s) {\n"
     b"            *p = s->reg_next;\n"
     b"            s->reg_next = NULL;\n"
     b"            return;\n"
     b"        }\n"
     b"}\n"
     b"\n"
     b"static void wl_reg_remove(vkb_surface *s)\n"
     b"{\n"
     b"    pthread_mutex_lock(&wl_reg_lock);\n"
     b"    wl_reg_unlink_locked(s);\n"
     b"    pthread_mutex_unlock(&wl_reg_lock);\n"
     b"}\n"
     b"\n"
     b"/* Marks the surface and its swapchains: no request may name the wl_surface any more. `s->lock`\n"
     b" * held (the same order vkDestroySurfaceKHR has always used: s->lock -> sc->lock). */\n"
     b"static void surface_mark_gone_locked(vkb_surface *s)\n"
     b"{\n"
     b"    s->dead = 1;\n"
     b"    for (vkb_swapchain *sc = s->chains; sc; sc = sc->next) {\n"
     b"        pthread_mutex_lock(&sc->lock);\n"
     b"        __atomic_store_n(&sc->surface_gone, 1, __ATOMIC_RELEASE);\n"
     b"        if (sc->status >= 0) sc->status = VK_ERROR_SURFACE_LOST_KHR;\n"
     b"        pthread_cond_broadcast(&sc->pw_cond);\n"
     b"        pthread_cond_broadcast(&sc->late_cond);\n"
     b"        pthread_mutex_unlock(&sc->lock);\n"
     b"    }\n"
     b"}\n"
     b"\n"
     b"/* A present that already holds present_mu finishes its attach and commit first; the destroy\n"
     b" * request is queued after this returns. Bounded: a flush stuck on a full socket must not hang\n"
     b" * the app's own wl_surface_destroy. */\n"
     b"static void surface_drain_present(vkb_surface *s)\n"
     b"{\n"
     b"    struct timespec deadline;\n"
     b"    clock_gettime(CLOCK_REALTIME, &deadline);\n"
     b"    deadline.tv_nsec += 100000000L;\n"
     b"    if (deadline.tv_nsec >= 1000000000L) {\n"
     b"        deadline.tv_sec++;\n"
     b"        deadline.tv_nsec -= 1000000000L;\n"
     b"    }\n"
     b"    if (pthread_mutex_timedlock(&s->present_mu, &deadline) == 0) {\n"
     b"        pthread_mutex_unlock(&s->present_mu);\n"
     b"    } else if (errno == ETIMEDOUT && wl_gone_trace()) {\n"
     b"        fprintf(stderr, \"vkb-wsi: a present still naming the wl_surface after 100 ms\\n\");\n"
     b"    }\n"
     b"}\n"
     b"\n"
     b"/* The shim's call: only the marks, no Vulkan object is destroyed here (the app still owns the\n"
     b" * swapchain and its buffers, and destroying them early is a use-after-free). */\n"
     b"VKB_EXPORT void vkb_wayland_surface_gone(struct wl_surface *surface)\n"
     b"{\n"
     b"    vkb_surface *marked[8];\n"
     b"    int n = 0;\n"
     b"    if (!surface) return;\n"
     b"    pthread_mutex_lock(&wl_reg_lock);\n"
     b"    for (vkb_surface *s = wl_reg, *next; s; s = next) {\n"
     b"        next = s->reg_next;\n"
     b"        if (s->wl_surface != surface) continue;\n"
     b"        pthread_mutex_lock(&s->lock);\n"
     b"        surface_mark_gone_locked(s);\n"
     b"        /* libwayland frees the proxy next; the pointer may be handed out to a new surface. */\n"
     b"        s->wl_surface = NULL;\n"
     b"        s->refs++; /* held across the drain below */\n"
     b"        wl_reg_unlink_locked(s);\n"
     b"        if (n < (int)(sizeof(marked) / sizeof(marked[0]))) marked[n++] = s;\n"
     b"        pthread_mutex_unlock(&s->lock);\n"
     b"    }\n"
     b"    pthread_mutex_unlock(&wl_reg_lock);\n"
     b"    if (wl_gone_trace()) fprintf(stderr, \"vkb-wsi: wl_surface destroyed first: %d surface(s) marked\\n\", n);\n"
     b"    if (n == (int)(sizeof(marked) / sizeof(marked[0]))) {\n"
     b"        static int told;\n"
     b"        if (!__atomic_exchange_n(&told, 1, __ATOMIC_RELAXED))\n"
     b"            VKB_WARN(\"Wayland: more than %d VkSurfaces on one wl_surface; the extra ones only marked\",\n"
     b"                     (int)(sizeof(marked) / sizeof(marked[0])));\n"
     b"    }\n"
     b"    for (int i = 0; i < n; i++) {\n"
     b"        surface_drain_present(marked[i]);\n"
     b"        surface_unref(marked[i]);\n"
     b"    }\n"
     b"}\n"),
    (b"    if (r == VK_SUCCESS) {\n"
     b"        s->wl_display = pCreateInfo->display;\n"
     b"        s->wl_surface = pCreateInfo->surface;\n"
     b"    }\n",
     b"    if (r == VK_SUCCESS) {\n"
     b"        s->wl_display = pCreateInfo->display;\n"
     b"        s->wl_surface = pCreateInfo->surface;\n"
     b"        if (s->wl_surface) wl_reg_add(s);\n"
     b"    }\n"),
    (b"    vkb_surface *s = surf(surface);\n"
     b"    if (!s) return;\n",
     b"    vkb_surface *s = surf(surface);\n"
     b"    if (!s) return;\n"
     b"    /* Out of the shim's registry first: the wl_surface may already be gone, and a pointer that\n"
     b"     * libwayland hands out again must not find this surface. */\n"
     b"    wl_reg_remove(s);\n"),
    (b"    pthread_mutex_lock(&s->lock);\n"
     b"    s->dead = 1;\n"
     b"    for (vkb_swapchain *sc = s->chains; sc; sc = sc->next) {\n"
     b"        pthread_mutex_lock(&sc->lock);\n"
     b"        if (sc->status >= 0) sc->status = VK_ERROR_SURFACE_LOST_KHR;\n"
     b"        pthread_cond_broadcast(&sc->pw_cond);\n"
     b"        pthread_cond_broadcast(&sc->late_cond);\n"
     b"        pthread_mutex_unlock(&sc->lock);\n"
     b"    }\n"
     b"    pthread_mutex_unlock(&s->lock);\n"
     b"    surface_unref(s);\n",
     b"    pthread_mutex_lock(&s->lock);\n"
     b"    surface_mark_gone_locked(s);\n"
     b"    pthread_mutex_unlock(&s->lock);\n"
     b"    surface_drain_present(s); /* the app may destroy the wl_surface next */\n"
     b"    surface_unref(s);\n"),
    (b"        /* The app destroyed the surface: never attach or commit to it (the compositor would end\n"
     b"         * the connection over the invalid object), and fail the present as Vulkan says. */\n"
     b"        if (vkb_surface_is_dead(sc->surface)) r = VK_ERROR_SURFACE_LOST_KHR;\n",
     b"        /* The app destroyed the surface: never attach or commit to it (the compositor would end\n"
     b"         * the connection over the invalid object), and fail the present as Vulkan says. */\n"
     b"        if (__atomic_load_n(&sc->surface_gone, __ATOMIC_ACQUIRE) || vkb_surface_is_dead(sc->surface))\n"
     b"            r = VK_ERROR_SURFACE_LOST_KHR;\n"),
]

WSI_WAYLAND = [
    (b"    /* The app destroyed the wl_surface: nothing may name it again. */\n"
     b"    if (vkb_surface_is_dead(sc->surface)) return VK_ERROR_SURFACE_LOST_KHR;\n",
     b"    /* The app destroyed the wl_surface: nothing may name it again. */\n"
     b"    if (__atomic_load_n(&sc->surface_gone, __ATOMIC_ACQUIRE) || vkb_surface_is_dead(sc->surface))\n"
     b"        return VK_ERROR_SURFACE_LOST_KHR;\n"),
    (b"    if (sc->info.presentMode == VK_PRESENT_MODE_FIFO_KHR || sc->info.presentMode == VK_PRESENT_MODE_FIFO_RELAXED_KHR) {\n"
     b"        if (s->frame) wl.proxy_destroy(s->frame);\n"
     b"        s->frame = wl.proxy_marshal_constructor(s->surface_wrapper, WL_SURFACE_FRAME, wl.callback_iface, NULL);\n"
     b"        wl.proxy_add_listener(s->frame, (void (**)(void))frame_listener, s);\n"
     b"    }\n"
     b"    /* When the compositor shows this frame: for the latency log (the presenting thread's frame, or the\n"
     b"     * app's call's) and for a present wait (the presenting thread's only: it alone takes the events\n"
     b"     * as they come; a copy present counts as shown when handed over, wsi.c). */\n"
     b"    int on_thread = s->thread_on && pthread_equal(pthread_self(), s->thread);\n"
     b"    uint32_t lat = on_thread ? s->lat_cur : sc->lat_n;\n"
     b"    uint64_t pid = on_thread && !vkb_wsi_present_wait_commit(sc->dev->pd) ? s->id_cur : 0;\n"
     b"    for (int i = 0; i < 16 && (lat || pid) && s->presentation; i++)\n"
     b"        if (!s->lat_fb[i]) {\n"
     b"            struct wl_proxy *fb = wl.proxy_marshal_constructor(s->presentation, WP_PRESENTATION_FEEDBACK, &fb_iface, s->surface_wrapper, NULL);\n"
     b"            if (fb) {\n"
     b"                wl.proxy_add_listener(fb, (void (**)(void))fb_listener, s);\n"
     b"                s->lat_fb[i] = fb;\n"
     b"                s->lat_fb_n[i] = lat;\n"
     b"                s->lat_fb_id[i] = pid;\n"
     b"                s->fb_for_id = pid != 0;\n"
     b"                vkb_lat_flag(lat, VKB_LAT_F_FEEDBACK);\n"
     b"            }\n"
     b"            break;\n"
     b"        }\n"
     b"    WSI_TRACE(\"attach wl_surface#%u <- wl_buffer#%u, commit\", proxy_id(s->surface_wrapper), proxy_id(buffer));\n"
     b"    wl.proxy_marshal(s->surface_wrapper, WL_SURFACE_ATTACH, buffer, 0, 0);\n"
     b"    int v4 = wl.proxy_get_version(s->surface_wrapper) >= 4;\n"
     b"    if (region && region->rectangleCount && region->pRectangles) {\n"
     b"        for (uint32_t i = 0; i < region->rectangleCount; i++) {\n"
     b"            const VkRectLayerKHR *r = &region->pRectangles[i];\n"
     b"            wl.proxy_marshal(s->surface_wrapper, v4 ? WL_SURFACE_DAMAGE_BUFFER : WL_SURFACE_DAMAGE, r->offset.x, r->offset.y,\n"
     b"                             (int32_t)r->extent.width, (int32_t)r->extent.height);\n"
     b"        }\n"
     b"    } else {\n"
     b"        wl.proxy_marshal(s->surface_wrapper, v4 ? WL_SURFACE_DAMAGE_BUFFER : WL_SURFACE_DAMAGE, 0, 0, (int32_t)w, (int32_t)h);\n"
     b"    }\n"
     b"    wl.proxy_marshal(s->surface_wrapper, WL_SURFACE_COMMIT);\n"
     b"    wl.display_flush(s->display);\n"
     b"    if (wl.display_dispatch_queue_pending(s->display, s->queue) < 0) return VK_ERROR_SURFACE_LOST_KHR;\n"
     b"    return VK_SUCCESS;\n"
     b"}\n",
     b"    /* When the compositor shows this frame: for the latency log (the presenting thread's frame, or the\n"
     b"     * app's call's) and for a present wait (the presenting thread's only: it alone takes the events\n"
     b"     * as they come; a copy present counts as shown when handed over, wsi.c). */\n"
     b"    int on_thread = s->thread_on && pthread_equal(pthread_self(), s->thread);\n"
     b"    uint32_t lat = on_thread ? s->lat_cur : sc->lat_n;\n"
     b"    uint64_t pid = on_thread && !vkb_wsi_present_wait_commit(sc->dev->pd) ? s->id_cur : 0;\n"
     b"    /* Everything below names the wl_surface. vkb_wayland_surface_gone() takes present_mu before\n"
     b"     * libwayland queues wl_surface.destroy, so the destroy request is never overtaken by an\n"
     b"     * attach or a commit: a present that gets the lock after that call sees surface_gone and\n"
     b"     * sends nothing. No `s->lock` in here (the hook takes it before present_mu). */\n"
     b"    pthread_mutex_lock(&sc->surface->present_mu);\n"
     b"    if (__atomic_load_n(&sc->surface_gone, __ATOMIC_ACQUIRE)) {\n"
     b"        pthread_mutex_unlock(&sc->surface->present_mu);\n"
     b"        return VK_ERROR_SURFACE_LOST_KHR;\n"
     b"    }\n"
     b"    if (sc->info.presentMode == VK_PRESENT_MODE_FIFO_KHR || sc->info.presentMode == VK_PRESENT_MODE_FIFO_RELAXED_KHR) {\n"
     b"        if (s->frame) wl.proxy_destroy(s->frame);\n"
     b"        s->frame = wl.proxy_marshal_constructor(s->surface_wrapper, WL_SURFACE_FRAME, wl.callback_iface, NULL);\n"
     b"        wl.proxy_add_listener(s->frame, (void (**)(void))frame_listener, s);\n"
     b"    }\n"
     b"    for (int i = 0; i < 16 && (lat || pid) && s->presentation; i++)\n"
     b"        if (!s->lat_fb[i]) {\n"
     b"            struct wl_proxy *fb = wl.proxy_marshal_constructor(s->presentation, WP_PRESENTATION_FEEDBACK, &fb_iface, s->surface_wrapper, NULL);\n"
     b"            if (fb) {\n"
     b"                wl.proxy_add_listener(fb, (void (**)(void))fb_listener, s);\n"
     b"                s->lat_fb[i] = fb;\n"
     b"                s->lat_fb_n[i] = lat;\n"
     b"                s->lat_fb_id[i] = pid;\n"
     b"                s->fb_for_id = pid != 0;\n"
     b"                vkb_lat_flag(lat, VKB_LAT_F_FEEDBACK);\n"
     b"            }\n"
     b"            break;\n"
     b"        }\n"
     b"    WSI_TRACE(\"attach wl_surface#%u <- wl_buffer#%u, commit\", proxy_id(s->surface_wrapper), proxy_id(buffer));\n"
     b"    wl.proxy_marshal(s->surface_wrapper, WL_SURFACE_ATTACH, buffer, 0, 0);\n"
     b"    int v4 = wl.proxy_get_version(s->surface_wrapper) >= 4;\n"
     b"    if (region && region->rectangleCount && region->pRectangles) {\n"
     b"        for (uint32_t i = 0; i < region->rectangleCount; i++) {\n"
     b"            const VkRectLayerKHR *r = &region->pRectangles[i];\n"
     b"            wl.proxy_marshal(s->surface_wrapper, v4 ? WL_SURFACE_DAMAGE_BUFFER : WL_SURFACE_DAMAGE, r->offset.x, r->offset.y,\n"
     b"                             (int32_t)r->extent.width, (int32_t)r->extent.height);\n"
     b"        }\n"
     b"    } else {\n"
     b"        wl.proxy_marshal(s->surface_wrapper, v4 ? WL_SURFACE_DAMAGE_BUFFER : WL_SURFACE_DAMAGE, 0, 0, (int32_t)w, (int32_t)h);\n"
     b"    }\n"
     b"    wl.proxy_marshal(s->surface_wrapper, WL_SURFACE_COMMIT);\n"
     b"    wl.display_flush(s->display);\n"
     b"    pthread_mutex_unlock(&sc->surface->present_mu);\n"
     b"    if (wl.display_dispatch_queue_pending(s->display, s->queue) < 0) return VK_ERROR_SURFACE_LOST_KHR;\n"
     b"    return VK_SUCCESS;\n"
     b"}\n"),
    (b"    if (s->surface_wrapper && !vkb_surface_is_dead(sc->surface)) {\n"
     b"        WSI_TRACE(\"detach wl_surface#%u, commit\", proxy_id(s->surface_wrapper));\n"
     b"        wl.proxy_marshal(s->surface_wrapper, WL_SURFACE_ATTACH, NULL, 0, 0);\n"
     b"        wl.proxy_marshal(s->surface_wrapper, WL_SURFACE_COMMIT);\n"
     b"        wl.display_flush(s->display);\n"
     b"    }\n",
     b"    if (s->surface_wrapper && !vkb_surface_is_dead(sc->surface)) {\n"
     b"        /* A detach queued after the destroy is the same \"invalid object\": skipped once the\n"
     b"         * shim has marked the surface, and serialised with a present in flight. */\n"
     b"        pthread_mutex_lock(&sc->surface->present_mu);\n"
     b"        if (!__atomic_load_n(&sc->surface_gone, __ATOMIC_ACQUIRE)) {\n"
     b"            WSI_TRACE(\"detach wl_surface#%u, commit\", proxy_id(s->surface_wrapper));\n"
     b"            wl.proxy_marshal(s->surface_wrapper, WL_SURFACE_ATTACH, NULL, 0, 0);\n"
     b"            wl.proxy_marshal(s->surface_wrapper, WL_SURFACE_COMMIT);\n"
     b"            wl.display_flush(s->display);\n"
     b"        }\n"
     b"        pthread_mutex_unlock(&sc->surface->present_mu);\n"
     b"    }\n"),
]

FILES = {
    "client/wsi.h": WSI_H,
    "client/wsi.c": WSI_C,
    "client/wsi_wayland.c": WSI_WAYLAND,
}


def git(root, *args):
    return subprocess.check_output(["git", "-C", str(root), *args], text=True).strip()


def canonical(data):
    return data.replace(b"\r\n", b"\n")


def sha(data):
    return hashlib.sha256(canonical(data)).hexdigest()


def replace(data, pairs, name):
    for old, new in pairs:
        if data.count(old) != 1:
            raise SystemExit(f"Exact patch anchor missing or duplicated in {name}: {old[:120]!r}")
        data = data.replace(old, new)
    return data


def restore_newlines(data, like):
    newline = b"\r\n" if b"\r\n" in like else b"\n"
    return canonical(data).replace(b"\n", newline)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("source", type=Path)
    ap.add_argument("--check", action="store_true", help="Validate the lifecycle candidate pre-state without writing")
    ap.add_argument("--verify-candidate", action="store_true", help="Validate exactly the expected candidate without writing")
    args = ap.parse_args()
    if args.check and args.verify_candidate:
        ap.error("Select at most one check mode")
    root = args.source.resolve()
    if git(root, "rev-parse", "HEAD") != REVISION:
        raise SystemExit("Refusing source revision other than " + REVISION)
    have = {name: (root / name).read_bytes() for name in FILES}
    if args.verify_candidate:
        for name, expected in POST.items():
            if sha(have[name]) != expected:
                raise SystemExit("Candidate content mismatch: " + name)
        if git(root, "diff", "--name-only", "HEAD").splitlines() != sorted(FILES):
            raise SystemExit("Unexpected tracked changes in the candidate")
        print("Verified the exact wl_surface-gone candidate (capacity + lifecycle included)")
        return
    for name, expected in PRE.items():
        if sha(have[name]) != expected:
            raise SystemExit("Pre-state is not the exact lifecycle candidate: " + name)
    expected = {name: replace(canonical(have[name]), FILES[name], name) for name in FILES}
    if args.check:
        print("Verified the lifecycle candidate pre-state; the wl_surface-gone patch applies on top")
        return
    for name, want in expected.items():
        if canonical(have[name]) == want:
            print(name, "already candidate")
            continue
        (root / name).write_bytes(restore_newlines(want, have[name]))
        print(name, "patched (sha256=" + hashlib.sha256((root / name).read_bytes()).hexdigest() + ")")
    changes = git(root, "diff", "--name-only", "HEAD").splitlines()
    if changes != sorted(FILES):
        raise SystemExit("Unexpected tracked changes: " + repr(changes))
    print("Applied the wl_surface-gone candidate")


if __name__ == "__main__":
    main()
