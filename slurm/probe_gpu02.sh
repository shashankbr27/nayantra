#!/usr/bin/env bash
# Survey gpu02 prerequisites; writes everything to ~/probe_out.txt (shared home).
exec > ~/probe_out.txt 2>&1
echo "RUN_AT=$(date)"; hostname
echo "=== subuid ==="; grep shashank /etc/subuid /etc/subgid
echo "=== userns ==="; sysctl user.max_user_namespaces
echo "=== cdi/hooks ==="; ls /etc/cdi/ 2>&1; ls /usr/share/containers/oci/hooks.d/ 2>&1
echo "=== nvidia-ctk ==="; command -v nvidia-ctk || echo none
echo "=== internet ==="
for u in https://pypi.org/simple/ https://omniverse-content-production.s3-us-west-2.amazonaws.com; do
  echo "$u -> $(curl -s -o /dev/null -w %{http_code} --max-time 5 "$u")"
done
echo "=== disks ==="; df -h / /tmp | tail -2
echo "=== ifaces ==="; hostname -I
echo "=== tools ==="; command -v tmux unsquashfs fusermount3 squashfuse 2>&1
echo "=== gpus ==="; nvidia-smi -L 2>&1 | head -3
echo "PROBE_END"
