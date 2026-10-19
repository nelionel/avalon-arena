#!/bin/bash
# Isolation probe for avalon-jail. Builds a throwaway two-seat layout under
# /opt/avalon/games/_jailtest and checks what seat p01 can and cannot reach.
# Each line prints PASS when the observed outcome matches the expectation.
set -u
T=/opt/avalon/games/_jailtest
mkdir -p $T/public $T/referee $T/players/p01/work $T/players/p01/home $T/players/p01/tmp \
         $T/players/p02/work $T/players/p02/home $T/players/p02/tmp
echo "public info" > $T/public/state.json
echo "TOP SECRET roles" > $T/referee/roles.json
echo "p01 private" > $T/players/p01/work/ROLE.md
echo "p02 private: I am Merlin" > $T/players/p02/work/ROLE.md
chmod 711 $T $T/players; chmod 755 $T/public; chmod 700 $T/referee; chmod 644 $T/public/state.json
chown -R 61001:61000 $T/players/p01; chown -R 61002:61000 $T/players/p02
chmod 711 $T/players/p01 $T/players/p02
chmod 700 $T/players/p01/work $T/players/p01/home $T/players/p01/tmp $T/players/p02/work $T/players/p02/home $T/players/p02/tmp

# Root-owned listener on a private port: the jail must not be able to reach it.
python3 -c "
import socket,time
s=socket.socket(); s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR,1)
s.bind(('127.0.0.1',18999)); s.listen(5); time.sleep(30)" &
LPID=$!
sleep 0.5

J=(/opt/avalon/runtime/avalon-jail --uid 61001 --gid 61000
   --rox /usr --rox /bin --rox /lib --rox /lib64 --ro /etc --ro /proc --ro /sys
   --dev /dev/null --dev /dev/urandom --dev /dev/pts --dev /dev/tty
   --ro $T/public --rw $T/players/p01/work --rw $T/players/p01/home --rw $T/players/p01/tmp
   --tcp 443 --tcp 53 --env HOME=$T/players/p01/home --env PATH=/usr/bin:/bin
   --cwd $T/players/p01/work --)

fails=0
probe() {  # probe <expect ok|deny> <name> <shell snippet>
  local expect="$1" name="$2" snippet="$3"
  out=$("${J[@]}" /bin/bash -c "$snippet" 2>&1); rc=$?
  if [ "$expect" = ok ]; then [ $rc -eq 0 ] && v=PASS || v=FAIL; else [ $rc -ne 0 ] && v=PASS || v=FAIL; fi
  [ $v = FAIL ] && fails=$((fails+1))
  printf "%s  %-4s %-34s rc=%-3s %s\n" "$v" "$expect" "$name" "$rc" "$(echo "$out" | tail -1 | cut -c1-80)"
}

probe ok   "identity"                 'id -u'
probe ok   "read own ROLE.md"         'cat ROLE.md'
probe ok   "write own notes"          'echo hi > notes.md && cat notes.md'
probe ok   "read public state"        "cat $T/public/state.json"
probe deny "write public"             "echo x > $T/public/hack.txt"
probe deny "read p02 ROLE.md"         "cat $T/players/p02/work/ROLE.md"
probe deny "list players dir"         "ls $T/players"
probe deny "read referee secret"      "cat $T/referee/roles.json"
probe deny "list /srv"                "ls /srv"
probe deny "list /root"               "ls /root"
probe deny "write /tmp"               "echo x > /tmp/avalon_covert_probe"
probe deny "read /proc/1/environ"     "head -c 20 /proc/1/environ"
probe deny "tcp to local service"     "exec 3<>/dev/tcp/127.0.0.1/18999 && echo connected"
probe ok   "tcp api.anthropic.com:443" "exec 3<>/dev/tcp/api.anthropic.com/443 && echo connected"
probe deny "udp bind"                 "python3 -I -c 'import socket; s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM); s.bind((\"127.0.0.1\",19001)); print(\"bound\")'"
probe deny "abstract unix bind"       "python3 -I -c 'import socket; s=socket.socket(socket.AF_UNIX); s.bind(\"\\0covert\"); print(\"bound\")'"
probe deny "sysv shared memory"       "python3 -I -c 'import ctypes,sys; r=ctypes.CDLL(None).shmget(1234,4096,0o1666); print(r); sys.exit(r<0)'"
probe deny "regain root"              "python3 -I -c 'import os; os.setuid(0); print(\"root\")'"
probe deny "signal referee (pid 1)"   "kill -0 1"

# Landlock on its own: run with p02's uid (Unix permissions WOULD allow reading p02's
# files) but under p01's Landlock policy. The kernel must still refuse.
J2=(/opt/avalon/runtime/avalon-jail --uid 61002 --gid 61000
    --rox /usr --rox /bin --rox /lib --rox /lib64 --ro /etc --ro /proc --ro /sys --dev /dev/null
    --ro $T/public --rw $T/players/p01/work --env PATH=/usr/bin:/bin --cwd $T/public --)
pre=$(setpriv --reuid=61002 --regid=61000 --clear-groups cat $T/players/p02/work/ROLE.md 2>&1); prc=$?
out=$("${J2[@]}" /bin/bash -c "cat $T/players/p02/work/ROLE.md" 2>&1); rc=$?
if [ $prc -eq 0 ] && [ $rc -ne 0 ]; then v=PASS; else v=FAIL; fails=$((fails+1)); fi
printf "%s  %-4s %-34s rc=%-3s %s\n" "$v" deny "Landlock alone (uid can read it)" "$rc" "without jail: rc=$prc; in jail: $(echo "$out" | tail -1 | cut -c1-60)"

kill $LPID 2>/dev/null; wait $LPID 2>/dev/null
echo "public dir after probes:"; ls -la $T/public
echo "failures: $fails"
exit $fails
