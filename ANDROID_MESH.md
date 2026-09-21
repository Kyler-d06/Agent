# Android execution nodes

The node now supports Termux on Android. It can offload **independent lightweight
scripts**: text processing, small data transformations, hashes, and short network
fetches. It does not pool phone RAM/GPUs into the desktop or distribute a single
model automatically. Install each script and its dependencies on the target node.

1. Install [Termux](https://github.com/termux/termux-app#installation) and
   [Termux:API](https://github.com/termux/termux-api) from the same distribution
   source so their signing keys match. Install
   [Tailscale for Android](https://tailscale.com/docs/install/android) and join the
   same private network as the core computer.
2. Copy `node_agent.py`, `mobile_runtime.py`, `platform_contracts.py`, and
   `requirements-node.txt` into `~/agent-node` in Termux. Keep the node software
   outside the scripts root. Copy `examples/mesh_probe.py` and its `.agent.json`
   sidecar into `~/scripts` for an initial end-to-end check.
3. Run in Termux:

```bash
pkg install python termux-api
cd ~/agent-node
python -m pip install -r requirements-node.txt
mkdir -p ~/scripts
export NODE_NAME=android-phone
export NODE_DEVICE_TYPE=android
read -rsp 'Mesh NODE_KEY: ' NODE_KEY; echo
export NODE_KEY
export NODE_ROOTS_JSON="{\"scripts\":\"$HOME/scripts\"}"
termux-battery-status
termux-wake-lock
python node_agent.py
```

Enter the same private `NODE_KEY` configured on the core. The node defaults to
port 5080. Permit only the core to reach that port through your private mesh;
the development node server binds all interfaces. Do not port-forward it publicly.
Set Termux battery usage to unrestricted if Android suspends it. Android may still
terminate background processes; phone jobs are best-effort, not durable workers.
Use `termux-wake-unlock` after stopping the node.

4. In the core dashboard's Nodes area, add `android-phone` with status URL
   `http://<phone-Tailscale-IP>:5080` (the base URL, without `/health`). Alternatively,
   post that `name` and `status_url` to `/api/nodes/add` with your owner API key.
5. Ask the assistant to inspect and run `mesh_probe.py` in the `scripts` root.
   `mesh_select_node` ranks eligible nodes, `mesh_offload_script` dispatches once,
   and `mesh_get_job` retrieves completion and output. Offloading uses the existing
   execution-grant policy. For repeated approved work, grant `mesh_offload_script`
   through `owner_cli.py`, constraining `root` and `path` to the intended script.

Android defaults: one job, 120-second limit, light workloads only, at least 25%
battery, plugged in, and battery temperature below 40°C. Missing battery telemetry
blocks admission. These are configurable operational defaults, not temperature
guarantees. The battery sensor is not a CPU temperature sensor. Admission is
rechecked on the node; running jobs are stopped on timeout or failed power checks,
with telemetry cached for up to ten seconds.

| Setting | Purpose |
| --- | --- |
| `NODE_MAX_JOBS` | Concurrent jobs; defaults to 1 on Android, 2 elsewhere |
| `NODE_MAX_JOB_SECONDS` | Job runtime limit; defaults to 120 on Android, 900 elsewhere |
| `NODE_REQUIRE_CHARGING` | Defaults to 1 on Android; 0 allows unplugged work |
| `NODE_MIN_BATTERY` | Minimum battery percentage; default 25 |
| `NODE_MAX_BATTERY_TEMP_C` | Admission/stop temperature; default 40 |
| `NODE_JOB_ENV` | Explicit comma-separated environment variables to pass to scripts |
| `NODE_MAX_OUTPUT_CHARS` | Retained output tail per stream; default 20000 |

Install `psutil` optionally for CPU/RAM telemetry. The node tolerates restricted
Android `/proc` access; unknown memory cannot be used as a verified RAM estimate.
Scripts run as the Termux user, not in a sandbox. Only mark scripts you trust as
`autonomous`. Other scripts default to `private`. Heavy GPU work and generated
capability sandbox tests should stay on suitable desktop/server nodes.
