/* crow-cli's private persistent PTY runtime.
 * Adapted from Crow's deleted crow-session.c (App/Services/SessionRuntime,
 * removed in Crow's working tree after commit 48cee8c): one detached daemon
 * per terminal id, system PTY, private Unix socket, bounded replay.
 * No multiplexer or third-party libraries. */
#define _GNU_SOURCE
#include <sys/types.h>
#include <sys/socket.h>
#include <sys/un.h>
#include <sys/ioctl.h>
#include <sys/stat.h>
#include <sys/wait.h>
#include <sys/file.h>
#include <arpa/inet.h>
#include <termios.h>
#include <poll.h>
#include <fcntl.h>
#include <signal.h>
#include <unistd.h>
#include <errno.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <limits.h>
#ifdef __APPLE__
#include <util.h>
#include <libproc.h>
#else
#include <pty.h>
#endif

#define VERSION "2"
#define PEERS 8
#define REPLAY (4 * 1024 * 1024)
#define MAX_FRAME (1024 * 1024)
#define MAX_QUEUE (8 * 1024 * 1024)
static volatile sig_atomic_t stopped, resized;
static char endpoint[sizeof(((struct sockaddr_un *)0)->sun_path)], exit_path[PATH_MAX], lock_path[PATH_MAX];
static struct termios saved_tty;
static int tty_saved;
static void signal_stop(int signo) { (void)signo; stopped = 1; }
static void signal_resize(int signo) { (void)signo; resized = 1; }
static void restore_tty(void) { if (tty_saved) tcsetattr(STDIN_FILENO, TCSANOW, &saved_tty); }
static void die(const char *message) { fprintf(stderr, "crow-cli session: %s (%s)\n", message, strerror(errno)); exit(1); }
static void nonblock(int fd) { fcntl(fd, F_SETFL, fcntl(fd, F_GETFL) | O_NONBLOCK); }
static int write_all(int fd, const void *bytes, size_t length) {
    const unsigned char *p = bytes;
    while (length) { ssize_t n = write(fd, p, length); if (n > 0) { p += n; length -= (size_t)n; }
        else if (n < 0 && errno == EINTR) continue; else return -1; }
    return 0;
}
static int send_frame(int fd, char kind, const void *bytes, uint32_t length) {
    unsigned char header[5]; uint32_t network = htonl(length);
    header[0] = (unsigned char)kind; memcpy(header + 1, &network, 4);
    return write_all(fd, header, sizeof(header)) || (length && write_all(fd, bytes, length)) ? -1 : 0;
}
static int secure_directory(const char *path) {
    struct stat st;
    if (mkdir(path, 0700) < 0 && errno != EEXIST) return -1;
    if (lstat(path, &st) < 0 || !S_ISDIR(st.st_mode) || st.st_uid != getuid() || (st.st_mode & 077)) { errno = EPERM; return -1; }
    return 0;
}
static void paths(const char *id) {
    if (strlen(id) != 36) { errno = EINVAL; die("invalid session ID"); }
    for (const char *p = id; *p; p++) if (!strchr("0123456789abcdefABCDEF-", *p)) { errno = EINVAL; die("invalid session ID"); }
    char root[PATH_MAX]; const char *override = getenv("CROWCLI_SESSION_ROOT") ? getenv("CROWCLI_SESSION_ROOT") : getenv("CROW_SESSION_ROOT");
    if (override) snprintf(root, sizeof(root), "%s", override);
    else snprintf(root, sizeof(root), "/tmp/crow-cli-sessions-%lu", (unsigned long)getuid());
    if (secure_directory(root) < 0) die("unsafe runtime directory");
    if (snprintf(endpoint, sizeof(endpoint), "%s/%s.sock", root, id) >= (int)sizeof(endpoint)) { errno = ENAMETOOLONG; die("runtime path too long"); }
    snprintf(exit_path, sizeof(exit_path), "%s/%s.exit", root, id);
    snprintf(lock_path, sizeof(lock_path), "%s/%s.lock", root, id);
}
static int connect_session(void) {
    int fd = socket(AF_UNIX, SOCK_STREAM, 0); if (fd < 0) return -1;
    struct sockaddr_un address = {0}; address.sun_family = AF_UNIX; strcpy(address.sun_path, endpoint);
    if (connect(fd, (struct sockaddr *)&address, sizeof(address)) < 0) { close(fd); return -1; }
    return fd;
}
struct peer { int fd; unsigned char *input, *output; size_t input_size, output_size, output_offset; int attached; };
static void drop(struct peer *p) { if (p->fd >= 0) close(p->fd); free(p->input); free(p->output); memset(p, 0, sizeof(*p)); p->fd = -1; }
static int queue(struct peer *p, char type, const void *data, uint32_t length) {
    size_t pending = p->output_size - p->output_offset;
    if (pending + 5 + length > MAX_QUEUE) return -1;
    if (p->output_offset) { memmove(p->output, p->output + p->output_offset, pending); p->output_size = pending; p->output_offset = 0; }
    unsigned char *next = realloc(p->output, pending + 5 + length); if (!next) return -1; p->output = next;
    uint32_t network = htonl(length); p->output[pending] = (unsigned char)type; memcpy(p->output + pending + 1, &network, 4);
    if (length) memcpy(p->output + pending + 5, data, length); p->output_size += 5 + length; return 0;
}
static void terminate_child(int master, pid_t child) {
    pid_t foreground = tcgetpgrp(master);
    if (foreground > 0 && foreground != child) kill(-foreground, SIGTERM);
    kill(-child, SIGTERM); kill(child, SIGTERM);
}
static void child_directory(pid_t child, const char *fallback, char *out, size_t capacity) {
    snprintf(out, capacity, "%s", fallback);
#ifdef __APPLE__
    struct proc_vnodepathinfo info;
    if (proc_pidinfo(child, PROC_PIDVNODEPATHINFO, 0, &info, sizeof(info)) == sizeof(info)) snprintf(out, capacity, "%s", info.pvi_cdir.vip_path);
#else
    char path[80]; snprintf(path, sizeof(path), "/proc/%ld/cwd", (long)child);
    ssize_t n = readlink(path, out, capacity - 1); if (n >= 0) out[n] = 0;
#endif
}
static void serve(const char *id, const char *directory, const char *command, struct winsize dimensions) {
    signal(SIGPIPE, SIG_IGN); signal(SIGHUP, SIG_IGN); signal(SIGTERM, signal_stop); signal(SIGINT, signal_stop);
    int listener = socket(AF_UNIX, SOCK_STREAM, 0); if (listener < 0) _exit(1);
    struct sockaddr_un address = {0}; address.sun_family = AF_UNIX; strcpy(address.sun_path, endpoint);
    unlink(endpoint);
    if (bind(listener, (struct sockaddr *)&address, sizeof(address)) < 0 || chmod(endpoint, 0600) < 0 || listen(listener, PEERS) < 0) _exit(1);
    fcntl(listener, F_SETFD, FD_CLOEXEC); nonblock(listener);
    int master; pid_t child = forkpty(&master, NULL, NULL, &dimensions);
    if (child < 0) { unlink(endpoint); _exit(1); }
    if (!child) {
        signal(SIGHUP, SIG_DFL); signal(SIGPIPE, SIG_DFL); signal(SIGTERM, SIG_DFL); signal(SIGINT, SIG_DFL);
        close(listener); if (chdir(directory) < 0) { perror("crow-cli session directory"); _exit(126); }
        setenv("CROWCLI_TERMINAL_ID", id, 1); setenv("CROW_TERMINAL_ID", id, 1); setenv("TERM", "xterm-256color", 1);
        const char *shell = getenv("SHELL"); if (!shell || !*shell) shell = "/bin/sh";
        if (command && *command) execl("/bin/sh", "sh", "-lc", command, (char *)NULL);
        else execl(shell, shell, "-l", (char *)NULL);
        _exit(127);
    }
    nonblock(master); fcntl(master, F_SETFD, FD_CLOEXEC);
    struct peer clients[PEERS]; memset(clients, 0, sizeof(clients)); for (int i = 0; i < PEERS; i++) clients[i].fd = -1;
    unsigned char *replay = malloc(REPLAY), *pending_input = NULL; size_t replay_size = 0, pending_size = 0;
    if (!replay) { terminate_child(master, child); unlink(endpoint); _exit(1); }
    int status = 0, finished = 0, shutdown_ticks = 0;
    while (!finished) {
        struct pollfd fds[PEERS + 2]; fds[0] = (struct pollfd){listener, POLLIN, 0}; fds[1] = (struct pollfd){master, POLLIN | (pending_size ? POLLOUT : 0), 0};
        for (int i = 0; i < PEERS; i++) fds[i + 2] = (struct pollfd){clients[i].fd, POLLIN | (clients[i].output_size > clients[i].output_offset ? POLLOUT : 0), 0};
        if (poll(fds, PEERS + 2, 100) < 0 && errno != EINTR) break;
        if (stopped) { if (!shutdown_ticks) terminate_child(master, child); if (++shutdown_ticks == 20) { kill(-child, SIGKILL); kill(child, SIGKILL); } }
        if (fds[0].revents & POLLIN) {
            int fd = accept(listener, NULL, NULL); if (fd >= 0) {
                nonblock(fd); fcntl(fd, F_SETFD, FD_CLOEXEC); int slot;
                for (slot = 0; slot < PEERS && clients[slot].fd >= 0; slot++);
                if (slot == PEERS) close(fd); else clients[slot].fd = fd;
            }
        }
        if (fds[1].revents & POLLOUT) {
            ssize_t n = write(master, pending_input, pending_size);
            if (n > 0) { memmove(pending_input, pending_input + n, pending_size - (size_t)n); pending_size -= (size_t)n; }
        }
        if (fds[1].revents & (POLLIN | POLLHUP)) {
            unsigned char bytes[32768]; ssize_t n = read(master, bytes, sizeof(bytes));
            if (n > 0) {
                if (replay_size + (size_t)n > REPLAY) { size_t remove = replay_size + (size_t)n - REPLAY; memmove(replay, replay + remove, replay_size - remove); replay_size -= remove; }
                memcpy(replay + replay_size, bytes, (size_t)n); replay_size += (size_t)n;
                for (int i = 0; i < PEERS; i++) if (clients[i].fd >= 0 && clients[i].attached && queue(&clients[i], 'O', bytes, (uint32_t)n)) drop(&clients[i]);
            }
        }
        for (int i = 0; i < PEERS; i++) {
            struct peer *p = &clients[i]; if (p->fd < 0) continue;
            if (fds[i + 2].revents & POLLIN) {
                unsigned char bytes[32768]; ssize_t n = read(p->fd, bytes, sizeof(bytes));
                if (n <= 0) { if (!n || (errno != EAGAIN && errno != EINTR)) drop(p); continue; }
                if (p->input_size + (size_t)n > MAX_FRAME + 5) { drop(p); continue; }
                unsigned char *next = realloc(p->input, p->input_size + (size_t)n); if (!next) { drop(p); continue; }
                p->input = next; memcpy(p->input + p->input_size, bytes, (size_t)n); p->input_size += (size_t)n;
                while (p->fd >= 0 && p->input_size >= 5) {
                    uint32_t length; memcpy(&length, p->input + 1, 4); length = ntohl(length);
                    if (length > MAX_FRAME) { drop(p); break; } if (p->input_size < 5 + length) break;
                    char kind = (char)p->input[0]; unsigned char *data = p->input + 5;
                    if (kind == 'A') {
                        p->attached = 1; queue(p, 'O', "\033c", 2);
                        size_t start = 0;
                        if (replay_size == REPLAY) { while (start < replay_size && replay[start] != '\n') start++; if (start < replay_size) start++; }
                        if (queue(p, 'O', replay + start, (uint32_t)(replay_size - start))) { drop(p); break; }
                    } else if (kind == 'W' && length == 4 && p->attached) {
                        uint16_t values[2]; memcpy(values, data, 4); struct winsize size = {0}; size.ws_col = ntohs(values[0]); size.ws_row = ntohs(values[1]);
                        if (size.ws_col && size.ws_row) { ioctl(master, TIOCSWINSZ, &size); pid_t foreground = tcgetpgrp(master); if (foreground > 0) kill(-foreground, SIGWINCH); }
                    } else if (kind == 'D' && p->attached && length) {
                        if (pending_size + length > MAX_FRAME) { drop(p); break; }
                        unsigned char *input = realloc(pending_input, pending_size + length); if (!input) { drop(p); break; }
                        pending_input = input; memcpy(pending_input + pending_size, data, length); pending_size += length;
                    } else if (kind == 'K') { stopped = 1; }
                    else if (kind == 'I') { char cwd[PATH_MAX]; child_directory(child, directory, cwd, sizeof(cwd)); queue(p, 'I', cwd, (uint32_t)strlen(cwd)); }
                    else if (kind != 'A' && kind != 'W' && kind != 'D') { drop(p); break; }
                    memmove(p->input, p->input + 5 + length, p->input_size - 5 - length); p->input_size -= 5 + length;
                }
            }
            if (p->fd >= 0 && fds[i + 2].revents & POLLOUT) {
                ssize_t n = write(p->fd, p->output + p->output_offset, p->output_size - p->output_offset);
                if (n > 0) p->output_offset += (size_t)n; else if (n < 0 && errno != EAGAIN && errno != EINTR) drop(p);
            }
            if (p->fd >= 0 && fds[i + 2].revents & (POLLERR | POLLHUP | POLLNVAL)) drop(p);
        }
        if (waitpid(child, &status, WNOHANG) == child) finished = 1;
    }
    if (!finished) { terminate_child(master, child); kill(-child, SIGKILL); waitpid(child, &status, 0); }
    /* waitpid may report exit while the PTY still holds the command's last
       output. Drain it before sending exit status, without waiting for a
       descendant that inherited the slave descriptor. */
    for (;;) {
        unsigned char bytes[32768]; ssize_t n = read(master, bytes, sizeof(bytes));
        if (n < 0 && errno == EINTR) continue;
        if (n <= 0) break;
        for (int i = 0; i < PEERS; i++) if (clients[i].fd >= 0 && clients[i].attached && queue(&clients[i], 'O', bytes, (uint32_t)n)) drop(&clients[i]);
    }
    int code = WIFEXITED(status) ? WEXITSTATUS(status) : 128 + (WIFSIGNALED(status) ? WTERMSIG(status) : SIGTERM);
    int marker = open(exit_path, O_WRONLY | O_CREAT | O_TRUNC, 0600); if (marker >= 0) { dprintf(marker, "%d\n", code); close(marker); }
    close(listener); close(master); unlink(endpoint);
    /* Flush pending output and exit status with a deadline; a stalled client
       must never prevent this session from terminating. */
    uint32_t network = htonl((uint32_t)code);
    for (int i = 0; i < PEERS; i++) if (clients[i].fd >= 0) queue(&clients[i], 'X', &network, 4);
    for (int tick = 0; tick < 20; tick++) {
        int pending = 0;
        for (int i = 0; i < PEERS; i++) { struct peer *p = &clients[i]; if (p->fd < 0 || p->output_size == p->output_offset) continue;
            pending = 1; ssize_t n = write(p->fd, p->output + p->output_offset, p->output_size - p->output_offset);
            if (n > 0) p->output_offset += (size_t)n; else if (errno != EAGAIN && errno != EINTR) drop(p);
        }
        if (!pending) break; usleep(10000);
    }
    for (int i = 0; i < PEERS; i++) drop(&clients[i]); free(replay); free(pending_input); _exit(0);
}
static int start_or_attach(const char *id, const char *directory, const char *command, struct winsize size) {
    int fd = connect_session(); if (fd >= 0) return fd;
    int lock = open(lock_path, O_RDWR | O_CREAT, 0600); if (lock < 0 || flock(lock, LOCK_EX) < 0) die("cannot lock session");
    fd = connect_session(); if (fd >= 0) { close(lock); return fd; }
    if (access(exit_path, F_OK) == 0) { fprintf(stderr, "crow-cli session has ended. Create a new window to start another session.\n"); close(lock); return -1; }
    pid_t launch = fork(); if (launch < 0) die("cannot start session service");
    if (!launch) {
        if (setsid() < 0) _exit(1); pid_t daemon = fork(); if (daemon < 0) _exit(1); if (daemon) _exit(0);
        int null = open("/dev/null", O_RDWR); for (int i = 0; i < 3; i++) dup2(null, i); if (null > 2) close(null);
        /* Release inherited launch locks and SSH/GUI sockets. Only the PTY
           service's own descriptors may keep an attachment alive. */
        long limit = sysconf(_SC_OPEN_MAX); if (limit < 0 || limit > 65536) limit = 65536;
        for (int i = 3; i < limit; i++) close(i);
        serve(id, directory, command, size); _exit(1);
    }
    waitpid(launch, NULL, 0);
    for (int i = 0; i < 200; i++) { fd = connect_session(); if (fd >= 0 || access(exit_path, F_OK) == 0) break; usleep(10000); }
    close(lock); return fd;
}
/* crow-cli addition: run the daemon in the foreground (the caller detaches
   it). The C attach client busy-loops when its stdin is /dev/null on macOS
   (poll reports an event read() never satisfies), so crow-cli's Python
   client spawns daemons through this mode and talks the frame protocol
   itself; `attach` remains for interactive/ssh use. */
static int serve_direct(const char *id, const char *directory, const char *command, struct winsize size) {
    int existing = connect_session(); if (existing >= 0) { close(existing); return 0; }
    int lock = open(lock_path, O_RDWR | O_CREAT, 0600); if (lock < 0 || flock(lock, LOCK_EX) < 0) die("cannot lock session");
    existing = connect_session(); if (existing >= 0) { close(existing); close(lock); return 0; }
    if (access(exit_path, F_OK) == 0) { fprintf(stderr, "crow-cli session has ended. Create a new window to start another session.\n"); close(lock); return 1; }
    /* serve() must not inherit the startup lock: other starters check the
       socket first, then the lock, so holding it while serving deadlocks
       a starter that arrived before the socket was bound. Fork: the child
       serves without the lock, the parent releases it once the socket is
       connectable and then just reaps the child. */
    pid_t child = fork(); if (child < 0) die("cannot start session service");
    if (!child) { close(lock); serve(id, directory, command, size); _exit(1); }
    int status = 0;
    for (int i = 0; i < 200; i++) {
        int fd = connect_session(); if (fd >= 0) { close(fd); break; }
        if (waitpid(child, &status, WNOHANG) == child) { close(lock); return 1; }
        usleep(10000);
    }
    close(lock); waitpid(child, &status, 0); return 0;
}
static void send_size(int fd) {
    struct winsize size = {0}; if (ioctl(STDIN_FILENO, TIOCGWINSZ, &size) < 0 || !size.ws_col || !size.ws_row) { size.ws_col = 80; size.ws_row = 24; }
    uint16_t values[2] = {htons(size.ws_col), htons(size.ws_row)}; send_frame(fd, 'W', values, 4);
}
int main(int argc, char **argv) {
    if (argc == 2 && !strcmp(argv[1], "--version")) { puts(VERSION); return 0; }
    if (argc < 3 || (strcmp(argv[1], "attach") && strcmp(argv[1], "stop") && strcmp(argv[1], "info") && strcmp(argv[1], "serve"))) { fputs("Usage: crow-session attach UUID DIRECTORY [COMMAND] | serve UUID DIRECTORY [COMMAND] | stop UUID | info UUID\n", stderr); return 2; }
    paths(argv[2]); signal(SIGPIPE, SIG_IGN);
    if (!strcmp(argv[1], "serve")) {
        if (argc < 4) return 2;
        struct winsize ssize = {0}; ssize.ws_col = 80; ssize.ws_row = 24;
        return serve_direct(argv[2], argv[3], argc > 4 ? argv[4] : NULL, ssize);
    }
    int attach = !strcmp(argv[1], "attach"), info = !strcmp(argv[1], "info");
    struct winsize size = {0}; ioctl(STDIN_FILENO, TIOCGWINSZ, &size); if (!size.ws_col) size.ws_col = 80; if (!size.ws_row) size.ws_row = 24;
    int fd = attach && argc >= 4 ? start_or_attach(argv[2], argv[3], argc > 4 ? argv[4] : NULL, size) : connect_session();
    if (fd < 0) { if (!attach && !info) return 0; fprintf(stderr, "crow-cli session is unavailable.\n"); return 1; }
    if (!attach && !info) { send_frame(fd, 'K', NULL, 0); close(fd); return 0; }
    if (attach) {
        signal(SIGTERM, signal_stop); signal(SIGINT, signal_stop); signal(SIGHUP, signal_stop); signal(SIGWINCH, signal_resize);
        if (isatty(STDIN_FILENO) && tcgetattr(STDIN_FILENO, &saved_tty) == 0) { struct termios raw = saved_tty; cfmakeraw(&raw); tcsetattr(STDIN_FILENO, TCSANOW, &raw); tty_saved = 1; atexit(restore_tty); }
        send_frame(fd, 'A', NULL, 0); send_size(fd);
    } else send_frame(fd, 'I', NULL, 0);
    unsigned char *input = NULL; size_t input_size = 0; int result = 0;
    while (!stopped) {
        if (resized) { resized = 0; send_size(fd); }
        struct pollfd fds[2] = {{fd, POLLIN, 0}, {attach ? STDIN_FILENO : -1, POLLIN, 0}};
        int count = poll(fds, 2, 100); if (count < 0) { if (errno == EINTR) continue; break; }
        if (fds[1].revents & (POLLIN | POLLHUP)) { unsigned char bytes[32768]; ssize_t n = read(STDIN_FILENO, bytes, sizeof(bytes)); if (n <= 0) break; if (send_frame(fd, 'D', bytes, (uint32_t)n)) break; }
        if (fds[0].revents & (POLLIN | POLLHUP)) {
            unsigned char bytes[32768]; ssize_t n = read(fd, bytes, sizeof(bytes)); if (n <= 0) break;
            unsigned char *next = realloc(input, input_size + (size_t)n); if (!next) break; input = next; memcpy(input + input_size, bytes, (size_t)n); input_size += (size_t)n;
            while (input_size >= 5) {
                uint32_t length; memcpy(&length, input + 1, 4); length = ntohl(length); if (length > MAX_QUEUE) { result = 1; stopped = 1; break; }
                if (input_size < 5 + length) break;
                char kind = (char)input[0];
                if (kind == 'O') { if (write_all(STDOUT_FILENO, input + 5, length)) stopped = 1; }
                else if (kind == 'I') { write_all(STDOUT_FILENO, input + 5, length); write_all(STDOUT_FILENO, "\n", 1); stopped = 1; }
                else if (kind == 'X' && length == 4) { uint32_t code; memcpy(&code, input + 5, 4); result = (int)ntohl(code); stopped = 1; }
                memmove(input, input + 5 + length, input_size - 5 - length); input_size -= 5 + length;
            }
        }
        if (fds[0].revents & (POLLERR | POLLNVAL)) break;
    }
    close(fd); free(input); restore_tty(); tty_saved = 0; return result;
}
