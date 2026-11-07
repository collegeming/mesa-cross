/* SPDX-License-Identifier: GPL-3.0-or-later
 * Copyright (C) 2026 ASEbrahim and MaliBridge contributors
 *
 * CI check for the wl_surface-gone shim, with no compositor: dlopen an ICD the way the Vulkan
 * loader does (RTLD_LOCAL), create a real wl_surface proxy on a socketpair display, destroy it.
 * With the shim preloaded and VKBRIDGE_WSI_TRACE=1, its hook must be found through
 * /proc/self/maps, called, and the ICD must report a registry miss (a no-op) on stderr.
 *
 * Exit 0 when the whole path ran; the caller greps the trace lines.
 */
#define _GNU_SOURCE
#include <dlfcn.h>
#include <stdio.h>
#include <string.h>
#include <sys/socket.h>
#include <unistd.h>
#include <wayland-client.h>

int main(int argc, char **argv)
{
    if (argc < 2) {
        fprintf(stderr, "usage: %s <libvulkan_droidbridge.so>\n", argv[0]);
        return 2;
    }
    if (!dlopen(argv[1], RTLD_NOW | RTLD_LOCAL)) {
        fprintf(stderr, "icd=FAIL %s\n", dlerror());
        return 1;
    }
    int fd[2];
    if (socketpair(AF_UNIX, SOCK_STREAM, 0, fd) != 0) return 3;
    struct wl_display *d = wl_display_connect_to_fd(fd[0]);
    if (!d) return 4;
    struct wl_proxy *reg = wl_proxy_marshal_constructor((struct wl_proxy *)d, WL_DISPLAY_GET_REGISTRY, &wl_registry_interface,
                                                        NULL);
    struct wl_proxy *surf = wl_proxy_marshal_constructor(reg, 0, &wl_surface_interface, NULL);
    if (!surf) return 5;
    if (wl_proxy_get_class(surf) == NULL || strcmp(wl_proxy_get_class(surf), "wl_surface") != 0) return 6;
    wl_surface_destroy((struct wl_surface *)surf);
    wl_display_flush(d);
    printf("wl-gone-smoke=OK\n");
    return 0;
}
