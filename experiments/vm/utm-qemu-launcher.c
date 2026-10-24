#include <dlfcn.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

extern char **environ;

typedef int (*tool_main_fn)(int, const char *[]);
typedef int (*qemu_init_fn)(int, const char *[], const char *[]);
typedef void (*qemu_loop_fn)(void);
typedef void (*qemu_cleanup_fn)(void);

int main(int argc, const char *argv[]) {
    const char *slash = strrchr(argv[0], '/');
    const char *program = slash == NULL ? argv[0] : slash + 1;
    const char *library = getenv(strcmp(program, "qemu-img") == 0
        ? "HB02_QEMU_IMG_FRAMEWORK"
        : "HB02_QEMU_SYSTEM_FRAMEWORK");
    if (library == NULL || library[0] != '/') {
        fputs("an absolute pinned QEMU framework path is required\n", stderr);
        return 1;
    }

    void *handle = dlopen(library, RTLD_LOCAL | RTLD_LAZY | RTLD_FIRST);
    if (handle == NULL) {
        fprintf(stderr, "failed to load %s: %s\n", library, dlerror());
        return 1;
    }

    tool_main_fn tool_main = dlsym(handle, "main");
    if (tool_main != NULL) {
        return tool_main(argc, argv);
    }

    qemu_init_fn qemu_init = dlsym(handle, "qemu_init");
    qemu_loop_fn qemu_main_loop = dlsym(handle, "qemu_main_loop");
    qemu_cleanup_fn qemu_cleanup = dlsym(handle, "qemu_cleanup");
    if (qemu_init == NULL || qemu_main_loop == NULL || qemu_cleanup == NULL) {
        fprintf(stderr, "QEMU framework is missing lifecycle symbols: %s\n", dlerror());
        return 1;
    }
    qemu_init(argc, argv, (const char **)environ);
    qemu_main_loop();
    qemu_cleanup();
    return 0;
}
