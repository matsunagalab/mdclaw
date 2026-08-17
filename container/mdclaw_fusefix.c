/* cuFFT workaround for FUSE-mounted container images.
 *
 * Some rootless container runtimes mount image files through FUSE.  On
 * affected CUDA driver/runtime combinations, cuFFT plan creation can fail
 * when the driver has to fault in pages from a FUSE-backed libcufft mapping.
 * Pre-populating the readable private writable mappings avoids that driver
 * page-fault path while leaving the pages file-backed and shareable.
 *
 * Loaded through LD_PRELOAD.  The constructor handles mappings present at
 * startup, while the dlopen() interposer handles libcufft arriving later as a
 * dependency of a CUDA plugin or framework library.
 *
 * Remove this workaround once the runtime can kernel-mount the image or the
 * underlying driver/FUSE interaction is fixed.
 */
#define _GNU_SOURCE
#include <dlfcn.h>
#include <stdio.h>
#include <string.h>
#include <sys/mman.h>

#ifndef MADV_POPULATE_READ
#define MADV_POPULATE_READ 22
#endif

/* Libraries whose file-backed mappings must be pre-faulted. */
static const char *const targets[] = { "libcufft.so", NULL };

#define MAX_FIXED 32
static unsigned long fixed_lo[MAX_FIXED], fixed_hi[MAX_FIXED];
static int fixed_n;

static int already_fixed(unsigned long lo, unsigned long hi)
{
    for (int i = 0; i < fixed_n; ++i)
        if (fixed_lo[i] == lo && fixed_hi[i] == hi)
            return 1;
    return 0;
}

static void mdclaw_fusefix(void)
{
    FILE *f = fopen("/proc/self/maps", "r");
    if (!f)
        return;

    char line[8192];
    while (fgets(line, sizeof line, f)) {
        unsigned long lo, hi;
        char perms[8];
        int off = 0;

        if (sscanf(line, "%lx-%lx %7s %n", &lo, &hi, perms, &off) < 3)
            continue;
        /* Private writable file mappings hold the device code. */
        if (perms[0] != 'r' || perms[1] != 'w' || perms[3] != 'p')
            continue;

        const char *path = strchr(line + off, '/');
        if (!path)
            continue;

        for (const char *const *t = targets; *t; ++t) {
            if (!strstr(path, *t))
                continue;
            if (already_fixed(lo, hi))
                break;
            madvise((void *)lo, hi - lo, MADV_POPULATE_READ);
            if (fixed_n < MAX_FIXED) {
                fixed_lo[fixed_n] = lo;
                fixed_hi[fixed_n] = hi;
                ++fixed_n;
            }
            break;
        }
    }
    fclose(f);
}

void *dlopen(const char *file, int mode)
{
    static void *(*real_dlopen)(const char *, int);
    if (!real_dlopen)
        real_dlopen = dlsym(RTLD_NEXT, "dlopen");
    void *handle = real_dlopen(file, mode);
    if (handle)
        mdclaw_fusefix();
    return handle;
}

__attribute__((constructor(101)))
static void mdclaw_fusefix_ctor(void) { mdclaw_fusefix(); }
