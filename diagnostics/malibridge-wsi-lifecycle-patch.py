#!/usr/bin/env python3
"""Apply/check the 47ec18e Wayland surface/wl_buffer lifecycle candidate.

Builds on the format-capacity patch (the device candidate): the pre-state may be pristine or
capacity-patched. Only client/wsi.h, client/wsi.c, client/wsi_wayland.c change.
"""
import argparse
import hashlib
import subprocess
from pathlib import Path

REVISION = "47ec18eaa9d9f83c359d56ba412a17d61ba59a66"
BLOBS = {
    "client/wsi.h": "2e11bb0f8f72c0862fcf0bbb8709ec2a049c07a9",
    "client/wsi.c": "9b897d79c6205afdd2c7c74100817aa1e0d41bdd",
    "client/wsi_wayland.c": "e43880a5d35f5f6be33e9619039fbf1af45c997d",
}
# The capacity patch (diagnostics/malibridge-format-capacity-patch.py) applied first in CI.
CAPACITY = [
    (b"dmabuf_mods[256];", b"dmabuf_mods[2048];"),
    (b"if (s->nmods < 256) {", b"if (s->nmods < (int)(sizeof(s->dmabuf_mods) / sizeof(s->dmabuf_mods[0]))) {"),
    (b"        s->nmods++;\n    }\n}\n\nstatic const void *dmabuf_listener[]", b"        s->nmods++;\n    } else {\n        static int told;\n        if (__atomic_fetch_add(&told, 1, __ATOMIC_RELAXED) == 0)\n            VKB_WARN(\"Wayland: dma-buf modifier table full (%u entries); later format/modifier pairs dropped\",\n                     (unsigned)(sizeof(s->dmabuf_mods) / sizeof(s->dmabuf_mods[0])));\n    }\n}\n\nstatic const void *dmabuf_listener[]"),
]

WSI_H = [
    (b"enum vkb_surface_kind { VKB_SURF_WAYLAND, VKB_SURF_XCB, VKB_SURF_HEADLESS };\n\ntypedef struct vkb_surface {",
     b"enum vkb_surface_kind { VKB_SURF_WAYLAND, VKB_SURF_XCB, VKB_SURF_HEADLESS };\n\ntypedef struct vkb_swapchain vkb_swapchain;\n\ntypedef struct vkb_surface {"),
    (b"typedef struct vkb_swapchain vkb_swapchain;\n\ntypedef struct vkb_swap_image {",
     b"typedef struct vkb_swap_image {"),
    (b"    struct vkb_wl_display_info *wl_info;   /* cached formats/modifiers per wl_display */\n} vkb_surface;",
     b"    struct vkb_wl_display_info *wl_info;   /* cached formats/modifiers per wl_display */\n"
     b"    /* vkDestroySurfaceKHR may be called before the swapchains on the surface (a game that quits\n"
     b"     * presents and destroys its window at the same time): the surface is marked dead, not freed,\n"
     b"     * and the memory goes with the last reference (wsi.c). With `lock`. */\n"
     b"    pthread_mutex_t lock;\n"
     b"    int refs;                /* the app's, and one per swapchain built on it */\n"
     b"    int dead;                /* no request may name its wl_surface any more */\n"
     b"    vkb_swapchain *chains;   /* the swapchains built on it (their `next`) */\n"
     b"} vkb_surface;"),
    (b"struct vkb_swapchain {\n    vkb_device *dev;\n    vkb_surface *surface;\n",
     b"struct vkb_swapchain {\n    vkb_device *dev;\n    vkb_surface *surface;\n"
     b"    vkb_swapchain *next;      /* the surface's list of its swapchains */\n"),
    (b"/* Helpers in wsi.c */\n",
     b"/* Helpers in wsi.c */\n"
     b"/* 1 once the app destroyed the surface: presents on its swapchains then fail with\n"
     b" * VK_ERROR_SURFACE_LOST_KHR instead of naming a destroyed wl_surface. */\n"
     b"int vkb_surface_is_dead(vkb_surface *s);\n"),
]

WSI_C = [
    (b"    vkb_surface *s = calloc(1, sizeof(*s));\n    if (!s) return VK_ERROR_OUT_OF_HOST_MEMORY;\n    s->kind = kind;",
     b"    vkb_surface *s = calloc(1, sizeof(*s));\n    if (!s) return VK_ERROR_OUT_OF_HOST_MEMORY;\n"
     b"    pthread_mutex_init(&s->lock, NULL);\n    s->refs = 1;\n    s->kind = kind;"),
    (b"    *out = (VkSurfaceKHR)(uintptr_t)s;\n    return VK_SUCCESS;\n}\n\nVKAPI_ATTR VkResult VKAPI_CALL vkb_ep_vkCreateWaylandSurfaceKHR",
     b"    *out = (VkSurfaceKHR)(uintptr_t)s;\n    return VK_SUCCESS;\n}\n\n"
     b"/* A swapchain's reference, and its place in the surface's list (vkDestroySurfaceKHR walks it). */\n"
     b"static void surface_chain_add(vkb_surface *s, vkb_swapchain *sc)\n"
     b"{\n"
     b"    pthread_mutex_lock(&s->lock);\n"
     b"    sc->next = s->chains;\n"
     b"    s->chains = sc;\n"
     b"    s->refs++;\n"
     b"    pthread_mutex_unlock(&s->lock);\n"
     b"}\n\n"
     b"/* Out of the surface's list first: vkDestroySurfaceKHR must not see a swapchain half torn down\n"
     b" * (its lock and condvars go next in swapchain_free). */\n"
     b"static void surface_chain_unlink(vkb_swapchain *sc)\n"
     b"{\n"
     b"    vkb_surface *s = sc->surface;\n"
     b"    if (!s) return;\n"
     b"    pthread_mutex_lock(&s->lock);\n"
     b"    for (vkb_swapchain **p = &s->chains; *p; p = &(*p)->next)\n"
     b"        if (*p == sc) {\n"
     b"            *p = sc->next;\n"
     b"            break;\n"
     b"        }\n"
     b"    pthread_mutex_unlock(&s->lock);\n"
     b"}\n\n"
     b"/* The swapchain's reference goes (the last one frees the surface). */\n"
     b"static void surface_unref(vkb_surface *s)\n"
     b"{\n"
     b"    if (!s) return;\n"
     b"    pthread_mutex_lock(&s->lock);\n"
     b"    int refs = --s->refs;\n"
     b"    pthread_mutex_unlock(&s->lock);\n"
     b"    if (!refs) {\n"
     b"        pthread_mutex_destroy(&s->lock);\n"
     b"        free(s);\n"
     b"    }\n"
     b"}\n\n"
     b"int vkb_surface_is_dead(vkb_surface *s)\n"
     b"{\n"
     b"    if (!s) return 0;\n"
     b"    pthread_mutex_lock(&s->lock);\n"
     b"    int dead = s->dead;\n"
     b"    pthread_mutex_unlock(&s->lock);\n"
     b"    return dead;\n"
     b"}\n\n"
     b"VKAPI_ATTR VkResult VKAPI_CALL vkb_ep_vkCreateWaylandSurfaceKHR"),
    (b"    (void)instance;\n    (void)pAllocator;\n    free(surf(surface));\n}",
     b"    (void)instance;\n    (void)pAllocator;\n"
     b"    vkb_surface *s = surf(surface);\n"
     b"    if (!s) return;\n"
     b"    /* Vulkan allows the surface to go before its swapchains, and a game that quits does exactly\n"
     b"     * that. Marked dead, not freed: the swapchains are told at once (their waits end) and every\n"
     b"     * later present fails with VK_ERROR_SURFACE_LOST_KHR - no attach or commit ever names the\n"
     b"     * destroyed wl_surface (a compositor's \"invalid object\" ends the whole connection). */\n"
     b"    pthread_mutex_lock(&s->lock);\n"
     b"    s->dead = 1;\n"
     b"    for (vkb_swapchain *sc = s->chains; sc; sc = sc->next) {\n"
     b"        pthread_mutex_lock(&sc->lock);\n"
     b"        if (sc->status >= 0) sc->status = VK_ERROR_SURFACE_LOST_KHR;\n"
     b"        pthread_cond_broadcast(&sc->pw_cond);\n"
     b"        pthread_cond_broadcast(&sc->late_cond);\n"
     b"        pthread_mutex_unlock(&sc->lock);\n"
     b"    }\n"
     b"    pthread_mutex_unlock(&s->lock);\n"
     b"    surface_unref(s);\n"
     b"}"),
    (b"static void swapchain_free(vkb_swapchain *sc)\n{\n    VkDevice device = (VkDevice)sc->dev;\n",
     b"static void swapchain_free(vkb_swapchain *sc)\n{\n    VkDevice device = (VkDevice)sc->dev;\n"
     b"    surface_chain_unlink(sc); /* first: vkDestroySurfaceKHR must not see it half torn down */\n"),
    (b"    pthread_cond_destroy(&sc->pw_cond);\n    pthread_cond_destroy(&sc->late_cond);\n    pthread_mutex_destroy(&sc->lock);\n    free(sc);",
     b"    pthread_cond_destroy(&sc->pw_cond);\n    pthread_cond_destroy(&sc->late_cond);\n    pthread_mutex_destroy(&sc->lock);\n"
     b"    surface_unref(sc->surface); /* its reference goes last */\n"
     b"    free(sc);"),
    (b"    sc->dev = dev;\n    sc->surface = s;\n    sc->info = *pCreateInfo;",
     b"    sc->dev = dev;\n    sc->surface = s;\n"
     b"    surface_chain_add(s, sc); /* the surface may be destroyed before this swapchain */\n"
     b"    sc->info = *pCreateInfo;"),
    (b"        VkResult r = sc->status;\n        if (index >= sc->count) r = VK_ERROR_OUT_OF_DATE_KHR;",
     b"        VkResult r = sc->status;\n"
     b"        /* The app destroyed the surface: never attach or commit to it (the compositor would end\n"
     b"         * the connection over the invalid object), and fail the present as Vulkan says. */\n"
     b"        if (vkb_surface_is_dead(sc->surface)) r = VK_ERROR_SURFACE_LOST_KHR;\n"
     b"        if (index >= sc->count) r = VK_ERROR_OUT_OF_DATE_KHR;"),
]

WSI_WAYLAND = [
    (b"    void (*proxy_set_queue)(struct wl_proxy *, struct wl_event_queue *);\n    uint32_t (*proxy_get_version)(struct wl_proxy *);",
     b"    void (*proxy_set_queue)(struct wl_proxy *, struct wl_event_queue *);\n    uint32_t (*proxy_get_version)(struct wl_proxy *);\n"
     b"    uint32_t (*proxy_get_id)(struct wl_proxy *);   /* optional: VKBRIDGE_WSI_TRACE's ids only */"),
    (b"            L(display_connect, \"wl_display_connect\");",
     b"            L(proxy_get_id, \"wl_proxy_get_id\"); /* optional: the trace's ids only */\n"
     b"            L(display_connect, \"wl_display_connect\");"),
    (b"int vkb_wl_load(void)\n{",
     b"/* VKBRIDGE_WSI_TRACE=1: each wl_surface and wl_buffer with its protocol id, and each attach and\n"
     b" * commit, on stderr. Off by default (a present is the hot path); it is for naming the object a\n"
     b" * compositor's \"invalid object N\" is about. */\n"
     b"static int wsi_trace(void)\n"
     b"{\n"
     b"    static int v = -1;\n"
     b"    if (v < 0) {\n"
     b"        const char *e = getenv(\"VKBRIDGE_WSI_TRACE\");\n"
     b"        v = e && *e && strcmp(e, \"0\") != 0;\n"
     b"    }\n"
     b"    return v;\n"
     b"}\n\n"
     b"static unsigned proxy_id(void *p) { return p && wl.proxy_get_id ? wl.proxy_get_id(p) : 0; }\n\n"
     b"#define WSI_TRACE(...) do { if (wsi_trace()) { fprintf(stderr, \"vkb-wsi: \" __VA_ARGS__); fputc('\\n', stderr); } } while (0)\n\n"
     b"int vkb_wl_load(void)\n{"),
    (b"static const void *dmabuf_listener[] = {(void *)dmabuf_format, (void *)dmabuf_modifier};\n\nstatic void buffer_release",
     b"static const void *dmabuf_listener[] = {(void *)dmabuf_format, (void *)dmabuf_modifier};\n\n"
     b"/* zwp_linux_buffer_params_v1: the compositor's answer to create_immed. A buffer it refused must\n"
     b" * not be attached at all (that first attach used to be where the failure showed, as an \"invalid\n"
     b" * object\" that ended the connection): VK_ERROR_FORMAT_NOT_SUPPORTED sends the swapchain to the\n"
     b" * wl_shm fallback instead. */\n"
     b"typedef struct params_answer {\n"
     b"    int created, failed;\n"
     b"} params_answer;\n\n"
     b"static void params_created(void *data, struct wl_proxy *params, struct wl_proxy *buffer)\n"
     b"{\n"
     b"    (void)params;\n"
     b"    (void)buffer;\n"
     b"    ((params_answer *)data)->created++;\n"
     b"}\n\n"
     b"static void params_failed(void *data, struct wl_proxy *params)\n"
     b"{\n"
     b"    (void)params;\n"
     b"    ((params_answer *)data)->failed++;\n"
     b"}\n\n"
     b"static const void *params_listener[] = {(void *)params_created, (void *)params_failed};\n\n"
     b"static void buffer_release"),
    (b"        s->buffers = calloc(sc->count, sizeof(*s->buffers));\n        for (uint32_t i = 0; i < sc->count; i++) {\n"
     b"            vkb_swap_image *img = &sc->images[i];\n"
     b"            struct wl_proxy *params = wl.proxy_marshal_constructor(s->dmabuf, DMABUF_CREATE_PARAMS, &params_iface, NULL);\n"
     b"            wl.proxy_marshal(params, PARAMS_ADD, img->dmabuf_fd, 0u, img->offset, img->stride, (uint32_t)(img->modifier >> 32),\n"
     b"                             (uint32_t)(img->modifier & 0xffffffffu));\n"
     b"            s->buffers[i] = wl.proxy_marshal_constructor(params, PARAMS_CREATE_IMMED, wl.buffer_iface, NULL, (int32_t)w, (int32_t)h, fourcc, 0u);\n"
     b"            wl.proxy_marshal(params, PARAMS_DESTROY);\n"
     b"            wl.proxy_destroy(params);\n"
     b"            wl.proxy_add_listener(s->buffers[i], (void (**)(void))buffer_listener, img);\n"
     b"            /* The buffer's AHardwareBuffer, once, before it is ever attached to the surface: the\n"
     b"             * server wrote its handle into this socket; the compositor reads it from the other end. */\n"
     b"            if (sc->ahb && s->ahb && img->ahb_sock >= 0) {\n"
     b"                wl.proxy_marshal(s->ahb, AHB_ATTACH, s->buffers[i], img->ahb_sock, w, h, img->stride / 4u, 0u, 0u, sc->count);\n"
     b"                close(img->ahb_sock); /* libwayland sent its own copy */\n"
     b"                img->ahb_sock = -1;\n"
     b"            }\n"
     b"        }\n"
     b"        if (wl.display_roundtrip_queue(s->display, s->queue) < 0 || wl.display_get_error(s->display)) {\n"
     b"            VKB_ERR(\"Wayland: the compositor refused the dma-bufs\");\n"
     b"            return VK_ERROR_SURFACE_LOST_KHR;\n"
     b"        }",
     b"        s->buffers = calloc(sc->count, sizeof(*s->buffers));\n"
     b"        struct wl_proxy **params_all = alloca(sc->count * sizeof(*params_all));\n"
     b"        params_answer answer = {0, 0};\n"
     b"        for (uint32_t i = 0; i < sc->count; i++) {\n"
     b"            vkb_swap_image *img = &sc->images[i];\n"
     b"            struct wl_proxy *params = wl.proxy_marshal_constructor(s->dmabuf, DMABUF_CREATE_PARAMS, &params_iface, NULL);\n"
     b"            wl.proxy_add_listener(params, (void (**)(void))params_listener, &answer);\n"
     b"            wl.proxy_marshal(params, PARAMS_ADD, img->dmabuf_fd, 0u, img->offset, img->stride, (uint32_t)(img->modifier >> 32),\n"
     b"                             (uint32_t)(img->modifier & 0xffffffffu));\n"
     b"            s->buffers[i] = wl.proxy_marshal_constructor(params, PARAMS_CREATE_IMMED, wl.buffer_iface, NULL, (int32_t)w, (int32_t)h, fourcc, 0u);\n"
     b"            /* The compositor's answer (failed: no buffer) comes with the roundtrip below, so the\n"
     b"             * params object stays until then. */\n"
     b"            params_all[i] = params;\n"
     b"            wl.proxy_add_listener(s->buffers[i], (void (**)(void))buffer_listener, img);\n"
     b"            WSI_TRACE(\"wl_buffer#%u for image %u (%ux%u %.4s, modifier 0x%llx)\", proxy_id(s->buffers[i]), i, w, h,\n"
     b"                      (char *)&fourcc, (unsigned long long)img->modifier);\n"
     b"        }\n"
     b"        int answered = wl.display_roundtrip_queue(s->display, s->queue);\n"
     b"        for (uint32_t i = 0; i < sc->count; i++) {\n"
     b"            wl.proxy_marshal(params_all[i], PARAMS_DESTROY);\n"
     b"            wl.proxy_destroy(params_all[i]);\n"
     b"        }\n"
     b"        if (answered < 0 || wl.display_get_error(s->display)) {\n"
     b"            VKB_ERR(\"Wayland: the compositor refused the dma-bufs\");\n"
     b"            return VK_ERROR_SURFACE_LOST_KHR;\n"
     b"        }\n"
     b"        if (answer.failed) {\n"
     b"            /* Destroy the refused buffers here, and let the swapchain fall back to wl_shm rather\n"
     b"             * than fail at its first attach (an invalid object ends the connection). */\n"
     b"            VKB_WARN(\"Wayland: the compositor refused %d of the %u dma-buf buffers; presenting through copies\", answer.failed,\n"
     b"                     sc->count);\n"
     b"            for (uint32_t i = 0; i < sc->count; i++)\n"
     b"                if (s->buffers[i]) {\n"
     b"                    wl.proxy_marshal(s->buffers[i], WL_BUFFER_DESTROY);\n"
     b"                    wl.proxy_destroy(s->buffers[i]);\n"
     b"                    s->buffers[i] = NULL;\n"
     b"                }\n"
     b"            return VK_ERROR_FORMAT_NOT_SUPPORTED;\n"
     b"        }\n"
     b"        /* The buffers' AHardwareBuffers, once, before any is attached to the surface: the server\n"
     b"         * wrote the handles into these sockets; the compositor reads them from the other end. */\n"
     b"        for (uint32_t i = 0; i < sc->count; i++) {\n"
     b"            vkb_swap_image *img = &sc->images[i];\n"
     b"            if (sc->ahb && s->ahb && img->ahb_sock >= 0) {\n"
     b"                wl.proxy_marshal(s->ahb, AHB_ATTACH, s->buffers[i], img->ahb_sock, w, h, img->stride / 4u, 0u, 0u, sc->count);\n"
     b"                close(img->ahb_sock); /* libwayland sent its own copy */\n"
     b"                img->ahb_sock = -1;\n"
     b"            }\n"
     b"        }"),
    (b"        wl.proxy_add_listener(s->shm_bufs[i].buffer, (void (**)(void))shm_buffer_listener, &s->shm_bufs[i]);",
     b"        wl.proxy_add_listener(s->shm_bufs[i].buffer, (void (**)(void))shm_buffer_listener, &s->shm_bufs[i]);\n"
     b"        WSI_TRACE(\"wl_shm buffer#%u\", proxy_id(s->shm_bufs[i].buffer));"),
    (b"    vkb_surface *surface = sc->surface;\n    wl_state *s = calloc(1, sizeof(*s));",
     b"    vkb_surface *surface = sc->surface;\n"
     b"    if (vkb_surface_is_dead(surface)) return VK_ERROR_SURFACE_LOST_KHR;\n"
     b"    wl_state *s = calloc(1, sizeof(*s));"),
    (b"    s->surface_wrapper = wl.proxy_create_wrapper(surface->wl_surface);\n    wl.proxy_set_queue(s->surface_wrapper, s->queue);",
     b"    s->surface_wrapper = wl.proxy_create_wrapper(surface->wl_surface);\n    wl.proxy_set_queue(s->surface_wrapper, s->queue);\n"
     b"    WSI_TRACE(\"connected, wl_surface#%u\", proxy_id(s->surface_wrapper));"),
    (b"    uint32_t w = sc->info.imageExtent.width, h = sc->info.imageExtent.height;\n    if (wl.display_get_error(s->display)) return VK_ERROR_SURFACE_LOST_KHR;",
     b"    uint32_t w = sc->info.imageExtent.width, h = sc->info.imageExtent.height;\n"
     b"    /* The app destroyed the wl_surface: nothing may name it again. */\n"
     b"    if (vkb_surface_is_dead(sc->surface)) return VK_ERROR_SURFACE_LOST_KHR;\n"
     b"    if (wl.display_get_error(s->display)) return VK_ERROR_SURFACE_LOST_KHR;"),
    (b"    wl.proxy_marshal(s->surface_wrapper, WL_SURFACE_ATTACH, buffer, 0, 0);",
     b"    WSI_TRACE(\"attach wl_surface#%u <- wl_buffer#%u, commit\", proxy_id(s->surface_wrapper), proxy_id(buffer));\n"
     b"    wl.proxy_marshal(s->surface_wrapper, WL_SURFACE_ATTACH, buffer, 0, 0);"),
    (b"static void wl_teardown(wl_state *s)\n{\n    if (!s) return;\n    vkb_swapchain *sc = s->sc;\n    if (s->frame) wl.proxy_destroy(s->frame);",
     b"/* The compositor's releases for the buffers it still holds, bounded: a wl_buffer destroyed while\n"
     b" * it is in use is a protocol error in some compositors. No present is pending here (wl_destroy\n"
     b" * has joined the presenting thread first). */\n"
     b"static void release_wait(wl_state *s, vkb_swapchain *sc)\n"
     b"{\n"
     b"    struct timespec t0, t;\n"
     b"    clock_gettime(CLOCK_MONOTONIC, &t0);\n"
     b"    for (;;) {\n"
     b"        int busy = 0;\n"
     b"        pthread_mutex_lock(&sc->lock);\n"
     b"        for (uint32_t i = 0; i < sc->count; i++) busy |= sc->images[i].busy;\n"
     b"        pthread_mutex_unlock(&sc->lock);\n"
     b"        if (!busy) return;\n"
     b"        clock_gettime(CLOCK_MONOTONIC, &t);\n"
     b"        if ((t.tv_sec - t0.tv_sec) * 1000 + (t.tv_nsec - t0.tv_nsec) / 1000000 > 100) {\n"
     b"            WSI_TRACE(\"buffers still held by the compositor after 100 ms\");\n"
     b"            return;\n"
     b"        }\n"
     b"        if (dispatch_timeout(s, 10000000ull) < 0) return;\n"
     b"    }\n"
     b"}\n\n"
     b"static void wl_teardown(wl_state *s)\n{\n    if (!s) return;\n    vkb_swapchain *sc = s->sc;\n"
     b"    /* Nothing attached first: the compositor has no buffer of ours left to read once the\n"
     b"     * wl_buffers go below. Skipped when the app destroyed the surface - that is exactly the\n"
     b"     * object a request must not name any more. */\n"
     b"    if (s->surface_wrapper && !vkb_surface_is_dead(sc->surface)) {\n"
     b"        WSI_TRACE(\"detach wl_surface#%u, commit\", proxy_id(s->surface_wrapper));\n"
     b"        wl.proxy_marshal(s->surface_wrapper, WL_SURFACE_ATTACH, NULL, 0, 0);\n"
     b"        wl.proxy_marshal(s->surface_wrapper, WL_SURFACE_COMMIT);\n"
     b"        wl.display_flush(s->display);\n"
     b"    }\n"
     b"    if (s->frame) wl.proxy_destroy(s->frame);"),
    (b"    if (s->buffers) {\n        for (uint32_t i = 0; i < sc->count; i++)\n            if (s->buffers[i]) {\n                wl.proxy_marshal(s->buffers[i], WL_BUFFER_DESTROY);\n                wl.proxy_destroy(s->buffers[i]);\n            }\n        free(s->buffers);\n    }",
     b"    if (s->buffers) {\n"
     b"        release_wait(s, sc); /* the compositor's last frames come back first (bounded) */\n"
     b"        for (uint32_t i = 0; i < sc->count; i++)\n"
     b"            if (s->buffers[i]) {\n"
     b"                WSI_TRACE(\"destroy wl_buffer#%u\", proxy_id(s->buffers[i]));\n"
     b"                wl.proxy_marshal(s->buffers[i], WL_BUFFER_DESTROY);\n"
     b"                wl.proxy_destroy(s->buffers[i]);\n"
     b"            }\n"
     b"        free(s->buffers);\n"
     b"    }"),
    (b"            if (s->shm_bufs[i].buffer) {\n                wl.proxy_marshal(s->shm_bufs[i].buffer, WL_BUFFER_DESTROY);",
     b"            if (s->shm_bufs[i].buffer) {\n"
     b"                WSI_TRACE(\"destroy wl_shm buffer#%u\", proxy_id(s->shm_bufs[i].buffer));\n"
     b"                wl.proxy_marshal(s->shm_bufs[i].buffer, WL_BUFFER_DESTROY);"),
    (b"    if (s->surface_wrapper) wl.proxy_wrapper_destroy(s->surface_wrapper);",
     b"    if (s->surface_wrapper) {\n"
     b"        WSI_TRACE(\"wl_surface#%u wrapper gone\", proxy_id(s->surface_wrapper));\n"
     b"        wl.proxy_wrapper_destroy(s->surface_wrapper);\n"
     b"    }"),
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


def blob_hash(data):
    data = canonical(data)
    return hashlib.sha1(b"blob " + str(len(data)).encode() + b"\0" + data).hexdigest()


def replace(data, pairs, name):
    for old, new in pairs:
        if data.count(old) != 1:
            raise SystemExit(f"Exact patch anchor missing or duplicated in {name}: {old[:120]!r}")
        data = data.replace(old, new)
    return data


def capacity(data):
    return replace(data, CAPACITY, "capacity pre-state")


def apply_wsi(data, name):
    return replace(data, FILES[name], name)


def restore_newlines(data, like):
    newline = b"\r\n" if b"\r\n" in like else b"\n"
    return canonical(data).replace(b"\n", newline)


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
    pristine = {name: subprocess.check_output(["git", "-C", str(root), "show", "HEAD:" + name]) for name in FILES}
    if args.check:
        for name, data in pristine.items():
            if canonical((root / name).read_bytes()) != canonical(data):
                raise SystemExit("Baseline is not pristine: " + name)
            apply_wsi(capacity(data) if name == "client/wsi_wayland.c" else data, name)
        print("Verified pristine baseline; the lifecycle patch applies on top of the capacity candidate")
        return
    expected = {}
    for name, data in pristine.items():
        # The device candidate already carries the format-capacity fix; accept either pre-state.
        expected[name] = apply_wsi(capacity(data) if name == "client/wsi_wayland.c" else data, name)
    if args.verify_candidate:
        for name, want in expected.items():
            if canonical((root / name).read_bytes()) != canonical(want):
                raise SystemExit("Candidate content mismatch: " + name)
        if git(root, "diff", "--name-only", "HEAD").splitlines() != sorted(FILES):
            raise SystemExit("Unexpected tracked changes in the candidate")
        print("Verified the exact lifecycle candidate (capacity fix included)")
        return
    for name, want in expected.items():
        path = root / name
        have = path.read_bytes()
        if canonical(have) == canonical(want):
            print(name, "already candidate")
            continue
        if name == "client/wsi_wayland.c":
            if canonical(have) == canonical(pristine[name]):
                pre_state = "pristine"
            elif canonical(have) == canonical(capacity(pristine[name])):
                pre_state = "capacity"
            else:
                raise SystemExit("Working tree is neither pristine nor the capacity candidate: " + name)
        else:
            if canonical(have) != canonical(pristine[name]):
                raise SystemExit("Working tree is not pristine: " + name)
            pre_state = "pristine"
        path.write_bytes(restore_newlines(want, have))
        print(name, "patched (" + pre_state + ", sha256=" + hashlib.sha256(path.read_bytes()).hexdigest() + ")")
    changes = git(root, "diff", "--name-only", "HEAD").splitlines()
    if changes != sorted(FILES):
        raise SystemExit("Unexpected tracked changes: " + repr(changes))
    print("Applied the client-only surface/wl_buffer lifecycle candidate")


if __name__ == "__main__":
    main()
