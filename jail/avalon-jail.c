// avalon-jail: run one Avalon player process in a kernel-enforced sandbox.
//
// Docker here forbids namespaces, so the "container" is built from primitives
// that work without CAP_SYS_ADMIN:
//   1. a dedicated uid/gid per seat (DAC isolation, no signal/ptrace across seats)
//   2. Landlock (ABI >= 4): filesystem allowlist + TCP connect allowlist, no TCP bind
//   3. seccomp: no bind/listen (no servers of any kind), no SysV/POSIX IPC, no
//      ptrace/process_vm, no keyrings, no namespace/mount syscalls, no io_uring
//   4. a scrubbed environment loaded from a root-only env file
//
// Usage:
//   avalon-jail --uid N --gid N [--ro P]... [--rox P]... [--rw P]... [--dev P]...
//               [--tcp PORT]... [--env-file F] [--env K=V]... [--cwd D]
//               [--nice N] [--chown-tty] [--report] -- CMD [ARGS...]
//
// Must be started as root; privileges are dropped before exec and cannot be
// regained (no_new_privs).

#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <grp.h>
#include <linux/audit.h>
#include <linux/filter.h>
#include <linux/landlock.h>
#include <linux/seccomp.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/prctl.h>
#include <sys/resource.h>
#include <sys/stat.h>
#include <sys/syscall.h>
#include <sys/types.h>
#include <unistd.h>

#define MAXP 64

static void die(const char *what) {
  fprintf(stderr, "avalon-jail: %s: %s\n", what, strerror(errno));
  exit(111);
}
static void dief(const char *what) {
  fprintf(stderr, "avalon-jail: %s\n", what);
  exit(111);
}

#define FS_FILE_RIGHTS                                                        \
  (LANDLOCK_ACCESS_FS_EXECUTE | LANDLOCK_ACCESS_FS_WRITE_FILE |                \
   LANDLOCK_ACCESS_FS_READ_FILE | LANDLOCK_ACCESS_FS_TRUNCATE)
#define FS_RO (LANDLOCK_ACCESS_FS_READ_FILE | LANDLOCK_ACCESS_FS_READ_DIR)
#define FS_ROX (FS_RO | LANDLOCK_ACCESS_FS_EXECUTE)
#define FS_DEV                                                                \
  (LANDLOCK_ACCESS_FS_READ_FILE | LANDLOCK_ACCESS_FS_WRITE_FILE |              \
   LANDLOCK_ACCESS_FS_READ_DIR | LANDLOCK_ACCESS_FS_TRUNCATE)
#define FS_ALL_V3                                                             \
  (LANDLOCK_ACCESS_FS_EXECUTE | LANDLOCK_ACCESS_FS_WRITE_FILE |                \
   LANDLOCK_ACCESS_FS_READ_FILE | LANDLOCK_ACCESS_FS_READ_DIR |                \
   LANDLOCK_ACCESS_FS_REMOVE_DIR | LANDLOCK_ACCESS_FS_REMOVE_FILE |            \
   LANDLOCK_ACCESS_FS_MAKE_CHAR | LANDLOCK_ACCESS_FS_MAKE_DIR |                \
   LANDLOCK_ACCESS_FS_MAKE_REG | LANDLOCK_ACCESS_FS_MAKE_SOCK |                \
   LANDLOCK_ACCESS_FS_MAKE_FIFO | LANDLOCK_ACCESS_FS_MAKE_BLOCK |              \
   LANDLOCK_ACCESS_FS_MAKE_SYM | LANDLOCK_ACCESS_FS_REFER |                    \
   LANDLOCK_ACCESS_FS_TRUNCATE)
// Writable trees: everything except creating device nodes, sockets and fifos.
#define FS_RW                                                                 \
  (FS_ALL_V3 & ~(LANDLOCK_ACCESS_FS_MAKE_CHAR | LANDLOCK_ACCESS_FS_MAKE_BLOCK | \
                 LANDLOCK_ACCESS_FS_MAKE_SOCK | LANDLOCK_ACCESS_FS_MAKE_FIFO))

struct rule {
  const char *path;
  uint64_t rights;
};

static struct rule rules[MAXP];
static int nrules;
static int ports[MAXP];
static int nports;
static char *envs[256];
static int nenvs;

static void add_rule(const char *p, uint64_t r) {
  if (nrules >= MAXP) dief("too many path rules");
  rules[nrules].path = p;
  rules[nrules].rights = r;
  nrules++;
}

static void add_env(char *kv) {
  if (!strchr(kv, '=')) dief("--env needs K=V");
  if (nenvs >= 250) dief("too many env vars");
  envs[nenvs++] = kv;
}

static void load_env_file(const char *path) {
  FILE *f = fopen(path, "r");
  if (!f) die("open env file");
  char *line = NULL;
  size_t cap = 0;
  ssize_t n;
  while ((n = getline(&line, &cap, f)) > 0) {
    while (n > 0 && (line[n - 1] == '\n' || line[n - 1] == '\r')) line[--n] = 0;
    if (n == 0 || line[0] == '#') continue;
    add_env(strdup(line));
  }
  free(line);
  fclose(f);
}

static void apply_landlock(void) {
  int abi = syscall(SYS_landlock_create_ruleset, NULL, 0,
                    LANDLOCK_CREATE_RULESET_VERSION);
  if (abi < 4) dief("Landlock ABI >= 4 required (filesystem + network rules)");

  struct landlock_ruleset_attr attr = {
      .handled_access_fs = FS_ALL_V3,
      .handled_access_net =
          LANDLOCK_ACCESS_NET_BIND_TCP | LANDLOCK_ACCESS_NET_CONNECT_TCP,
  };
  int rfd = syscall(SYS_landlock_create_ruleset, &attr, sizeof(attr), 0);
  if (rfd < 0) die("landlock_create_ruleset");

  for (int i = 0; i < nrules; i++) {
    int pfd = open(rules[i].path, O_PATH | O_CLOEXEC);
    if (pfd < 0) {
      fprintf(stderr, "avalon-jail: skip missing path %s\n", rules[i].path);
      continue;
    }
    struct stat st;
    if (fstat(pfd, &st) < 0) die("fstat");
    uint64_t r = rules[i].rights;
    if (!S_ISDIR(st.st_mode)) r &= FS_FILE_RIGHTS;
    struct landlock_path_beneath_attr pb = {.allowed_access = r,
                                            .parent_fd = pfd};
    if (syscall(SYS_landlock_add_rule, rfd, LANDLOCK_RULE_PATH_BENEATH, &pb, 0))
      die(rules[i].path);
    close(pfd);
  }
  for (int i = 0; i < nports; i++) {
    struct landlock_net_port_attr np = {
        .allowed_access = LANDLOCK_ACCESS_NET_CONNECT_TCP,
        .port = (uint64_t)ports[i]};
    if (syscall(SYS_landlock_add_rule, rfd, LANDLOCK_RULE_NET_PORT, &np, 0))
      die("landlock net rule");
  }
  if (syscall(SYS_landlock_restrict_self, rfd, 0)) die("landlock_restrict_self");
  close(rfd);
}

static const int denied_syscalls[] = {
    SYS_bind, SYS_listen, SYS_ptrace, SYS_process_vm_readv,
    SYS_process_vm_writev, SYS_shmget, SYS_shmat, SYS_shmctl, SYS_semget,
    SYS_semop, SYS_semctl, SYS_semtimedop, SYS_msgget, SYS_msgsnd, SYS_msgrcv,
    SYS_msgctl, SYS_mq_open, SYS_mq_unlink, SYS_mq_timedsend,
    SYS_mq_timedreceive, SYS_mq_notify, SYS_mq_getsetattr, SYS_keyctl,
    SYS_add_key, SYS_request_key, SYS_unshare, SYS_setns, SYS_mount,
    SYS_umount2, SYS_pivot_root, SYS_chroot, SYS_io_uring_setup,
    SYS_io_uring_enter, SYS_io_uring_register, SYS_perf_event_open, SYS_bpf,
    SYS_userfaultfd, SYS_kexec_load, SYS_kexec_file_load, SYS_init_module,
    SYS_finit_module, SYS_delete_module, SYS_acct, SYS_swapon, SYS_swapoff,
    SYS_reboot, SYS_sethostname, SYS_setdomainname, SYS_open_by_handle_at,
    SYS_name_to_handle_at, SYS_open_tree, SYS_move_mount, SYS_fsopen,
    SYS_fsconfig, SYS_fsmount, SYS_fspick,
};

static void apply_seccomp(void) {
  enum { N = sizeof(denied_syscalls) / sizeof(denied_syscalls[0]) };
  struct sock_filter f[6 + 2 * N + 1];
  int k = 0;
  f[k++] = (struct sock_filter)BPF_STMT(BPF_LD | BPF_W | BPF_ABS,
                                        offsetof(struct seccomp_data, arch));
  f[k++] = (struct sock_filter)BPF_JUMP(BPF_JMP | BPF_JEQ | BPF_K,
                                        AUDIT_ARCH_X86_64, 1, 0);
  f[k++] = (struct sock_filter)BPF_STMT(BPF_RET | BPF_K, SECCOMP_RET_KILL_PROCESS);
  f[k++] = (struct sock_filter)BPF_STMT(BPF_LD | BPF_W | BPF_ABS,
                                        offsetof(struct seccomp_data, nr));
  // Refuse the x32 ABI outright (it would alias syscall numbers).
  f[k++] = (struct sock_filter)BPF_JUMP(BPF_JMP | BPF_JGE | BPF_K, 0x40000000, 0, 1);
  f[k++] = (struct sock_filter)BPF_STMT(BPF_RET | BPF_K, SECCOMP_RET_KILL_PROCESS);
  for (int i = 0; i < N; i++) {
    f[k++] = (struct sock_filter)BPF_JUMP(BPF_JMP | BPF_JEQ | BPF_K,
                                          (unsigned)denied_syscalls[i], 0, 1);
    f[k++] = (struct sock_filter)BPF_STMT(BPF_RET | BPF_K,
                                          SECCOMP_RET_ERRNO | (EPERM & SECCOMP_RET_DATA));
  }
  f[k++] = (struct sock_filter)BPF_STMT(BPF_RET | BPF_K, SECCOMP_RET_ALLOW);
  struct sock_fprog prog = {.len = (unsigned short)k, .filter = f};
  if (prctl(PR_SET_SECCOMP, SECCOMP_MODE_FILTER, &prog, 0, 0)) die("seccomp");
}

int main(int argc, char **argv) {
  long uid = -1, gid = -1, nice_v = 10;
  const char *cwd = NULL;
  int chown_tty = 0, report = 0, i;

  for (i = 1; i < argc; i++) {
    const char *a = argv[i];
#define NEXT() (i + 1 < argc ? argv[++i] : (dief("missing value"), (char *)0))
    if (!strcmp(a, "--")) { i++; break; }
    else if (!strcmp(a, "--uid")) uid = atol(NEXT());
    else if (!strcmp(a, "--gid")) gid = atol(NEXT());
    else if (!strcmp(a, "--ro")) add_rule(NEXT(), FS_RO);
    else if (!strcmp(a, "--rox")) add_rule(NEXT(), FS_ROX);
    else if (!strcmp(a, "--rw")) add_rule(NEXT(), FS_RW);
    else if (!strcmp(a, "--dev")) add_rule(NEXT(), FS_DEV);
    else if (!strcmp(a, "--tcp")) {
      if (nports >= MAXP) dief("too many ports");
      ports[nports++] = atoi(NEXT());
    }
    else if (!strcmp(a, "--env-file")) load_env_file(NEXT());
    else if (!strcmp(a, "--env")) add_env(NEXT());
    else if (!strcmp(a, "--cwd")) cwd = NEXT();
    else if (!strcmp(a, "--nice")) nice_v = atol(NEXT());
    else if (!strcmp(a, "--chown-tty")) chown_tty = 1;
    else if (!strcmp(a, "--report")) report = 1;
    else { fprintf(stderr, "avalon-jail: unknown option %s\n", a); return 2; }
  }
  if (i >= argc) dief("no command given");
  if (uid < 1000 || gid < 1000) dief("--uid/--gid must be >= 1000");
  if (geteuid() != 0) dief("must start as root (drops to --uid)");

  // Close every inherited fd except stdio (tmux / referee descriptors).
  if (syscall(SYS_close_range, 3, ~0U, 0)) {
    for (int fd = 3; fd < 1024; fd++) close(fd);
  }

  if (chown_tty && isatty(0)) {
    if (fchown(0, (uid_t)uid, (gid_t)-1) || fchmod(0, 0600)) die("chown tty");
  }

  setpriority(PRIO_PROCESS, 0, (int)nice_v);
  struct rlimit core = {0, 0};
  setrlimit(RLIMIT_CORE, &core);
  struct rlimit nproc = {1024, 1024};
  setrlimit(RLIMIT_NPROC, &nproc);
  struct rlimit fsize = {256L << 20, 256L << 20};
  setrlimit(RLIMIT_FSIZE, &fsize);

  gid_t g = (gid_t)gid;
  if (setgroups(1, &g)) die("setgroups");
  if (setresgid(g, g, g)) die("setresgid");
  if (setresuid((uid_t)uid, (uid_t)uid, (uid_t)uid)) die("setresuid");
  if (setuid(0) == 0 || geteuid() == 0) dief("privilege drop failed");

  if (prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0)) die("no_new_privs");
  apply_landlock();
  apply_seccomp();

  if (cwd && chdir(cwd)) die("chdir");

  if (clearenv()) die("clearenv");
  for (int e = 0; e < nenvs; e++) {
    char *kv = envs[e];
    char *eq = strchr(kv, '=');
    *eq = 0;
    setenv(kv, eq + 1, 1);
    *eq = '=';
  }

  if (report) {
    fprintf(stderr, "avalon-jail: uid=%d gid=%d landlock(fs=%d rules, tcp=%d ports) seccomp=on nnp=on\n",
            getuid(), getgid(), nrules, nports);
  }
  execvp(argv[i], &argv[i]);
  die("exec");
  return 111;
}
