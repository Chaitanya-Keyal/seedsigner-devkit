# seedsigner-devkit

Tools for developing [SeedSigner](https://github.com/SeedSigner/seedsigner) on real
hardware:

- a Raspberry Pi running a [seedsigner-os](https://github.com/SeedSigner/seedsigner-os)
  `-dev` image: deploy a local checkout in one command, mirror the screen to an HDMI
  monitor, drive the app from a USB keyboard, read the app's log, and reach the device
  over Tailscale;
- an ESP32-P4 board running the SeedSigner MicroPython port: log, REPL, deploy and
  flash over the serial cable (see [ESP32-P4 boards](#esp32-p4-boards-micropython-port)).

None of this changes the app. It all lives outside the app's source tree.

| Path | Runs on | What it does |
| --- | --- | --- |
| `host/ss-deploy` | your machine | Syncs a checkout (clone or git worktree) to the device and restarts the app |
| `host/ss-device-setup` | your machine | Creates the app's venv if needed and installs the `device/` files |
| `device/sitecustomize.py` | device | HDMI mirror, USB keyboard as buttons, cropped LCD region, service log |
| `device/bashrc`, `device/bash_profile` | device | Root shell: prompt, history, aliases `ll`, `app`, `applog`, `logs` |
| `image/` | image build | Changes to seedsigner-os: Tailscale, console and reboot fixes, a `wifi.txt` example |
| `host/p4` | your machine (Linux; WSL adds usbipd attach) | Board log, REPL, overlay deploy and flashing for an ESP32-P4 on the MicroPython port |

Tested on a Pi Zero 2 W with the Waveshare 1.3" LCD HAT.

## Building the image

In a seedsigner-os checkout, before building with `--dev`:

```
cp -r <devkit>/image/rootfs-overlay-dev opt/
git apply <devkit>/image/pi02w-dev.patch
```

- `etc/init.d/S40tailscale`: starts Tailscale at boot (see below).
- `etc/profile.d/root-home.sh`: init starts the HDMI console shells with `HOME=/`, so
  they never read `/root/.bashrc`; this points them at `/root`.
- `pi02w-dev.patch` (other boards: make the same edits in their `board/` files):
  - comments out `dtoverlay=dwc2`. In host mode `dwc2` fails on a keyboard on an OTG
    adapter (`error -71`); the default `dwc_otg` driver works. This also turns off the
    USB-network gadget, so connect over Wi-Fi.
  - moves the kernel console from tty1 to tty2. init also starts a shell on tty1, so
    two shells shared that screen and split every keystroke.
  - adds the BCM2835 watchdog driver to the kernel. It's what restarts the board, so
    without it `reboot` halts instead.

## Setting up a freshly flashed card

1. On the boot partition: `authorized_keys` (your ssh public key), `wifi.txt` (see
   `image/boot/wifi.txt.example`: network name, password, two-letter country code, LF
   line endings; Pi Zero boards only see 2.4 GHz), and the Tailscale files below.
2. Boot it, then from your machine:

   ```
   ss-deploy <checkout> --no-restart    # the app's code
   ss-device-setup [--hdmi]             # venv (first run, ~3 minutes), the device files
   ```

   `--hdmi` turns on the HDMI mirror and keyboard (below); leave it out if you won't
   connect a monitor.

An ssh host for the device is assumed. Both scripts use `SEEDSIGNER_HOST` (default
`seedsigner-dev`):

```
Host seedsigner-dev
  User root
  IdentityFile ~/.ssh/id_ed25519
```

## Deploying a checkout

```
ss-deploy [checkout-dir] [--no-restart]
```

It rsyncs the tree with `-L`, so a git worktree's symlinked translations submodule
arrives as real files, then writes a minimal `.git` (HEAD, the branch ref, the origin
url and the one commit object, marked shallow) so the app's Version screen shows the
branch and commit. It compiles the translations and restarts the app. The device's
`venv/`, compiled `*.mo` files and `settings.json` are left alone.

The image's dropbear has no `sftp-server`, so plain `scp` fails. Use the legacy
protocol, `scp -O`, or `ssh seedsigner-dev 'cat > /path/to/file' < file`.

## Display, keyboard and logs

These come from `device/sitecustomize.py`, which `ss-device-setup` installs in the app's
venv (rerun it if you recreate the venv). Without its two config files on the data
partition it only writes the log; each file switches a feature on, is re-read while the
app runs, and can be deleted to switch it off again.

**HDMI mirror**: `/mnt/data/display-mirror` holds `SCALE VT`; `ss-device-setup --hdmi`
writes `1 3`. Each frame is drawn on the HDMI framebuffer inside a sketch of the device,
scaled up `SCALE` times (`auto` picks the largest whole scale that fits), while virtual
terminal `VT` is active. When the service starts the app (at boot, or after a deploy)
the monitor switches to that VT. Alt+F1 shows the shell and Alt+F3 the app again, or
`chvt 1` / `chvt 3` over SSH. The device's own LCD shows "Showing on HDMI monitor"
while the mirror is on screen. Changing Settings > Display type changes the sketch too.

**USB keyboard** (through an OTG adapter on the Pi's data port): while the mirror is on
and its VT active, the arrow keys are the joystick, Enter or Space is the joystick
press, and 1, 2 and 3 are KEY1-3. The sketch lights up pressed buttons. On any other VT
the keyboard only reaches the shell.

**Cropped LCD**: `/mnt/data/display-region` holds `SIZE X Y`. Every frame is scaled to
`SIZE`x`SIZE` and drawn at (`X`, `Y`) on the LCD, the rest black. Useful when part of
the panel is damaged.

**Logs**: the image starts the app with its output on `/dev/null`. The shim sends the
service's stdout and stderr to `/var/log/seedsigner.log` instead (RAM, so it's gone at
power-off), and keeps the previous run's log as `seedsigner.log.1`:

```
ssh seedsigner-dev tail -f /var/log/seedsigner.log
```

`logs` in a device shell does the same, and `applog` stops the service and runs the app
in the foreground (Ctrl+C, then `seedsigner start` to go back). `app` changes to the
app's directory, `/mnt/data/seedsigner`.

## Tailscale

`S40tailscale` lets you reach the device by name on whatever network it joins. It uses
Tailscale's static arm binaries instead of a Buildroot package, since this Buildroot
can't bootstrap Go on an arm64 build host. Put these on the boot partition:

- `tailscale/tailscale` and `tailscale/tailscaled` from
  `https://pkgs.tailscale.com/stable/tailscale_<version>_arm.tgz` (check the `.sha256`).
- `tailscale-authkey`: a single-use, pre-authorized auth key with a short expiry. Delete
  it from the card once the device shows up in your tailnet.

At boot the binaries are copied to `/mnt/data/tailscale` and `tailscaled` starts in
userspace-networking mode (the kernel has no TUN device), keeping its login state there.
The device joins as `seedsigner-dev`.

## Known issues with the -dev image

- On the Pi Zero 2 W the cpufreq driver fails to probe (error -95), so the CPU runs at
  600 MHz instead of 1 GHz, release images included
  ([seedsigner-os#129](https://github.com/SeedSigner/seedsigner-os/issues/129)).
  `force_turbo=1` in `config.txt` runs it at full speed.
- The dropbear host key is regenerated on every boot, so ssh warns about a changed key.
  `StrictHostKeyChecking accept-new` plus clearing the old entry, or a per-host
  `UserKnownHostsFile /dev/null`, avoids the prompt.
- With HDMI connected, the camera preview flashes on the monitor during camera tests.

## ESP32-P4 boards (MicroPython port)

`host/p4` wraps the everyday commands for a board running the SeedSigner MicroPython
port ([seedsigner-micropython-builder](https://github.com/kdmukai/seedsigner-micropython-builder)),
all over the board's USB-UART cable. Tested on the Waveshare ESP32-P4-WIFI6-Touch-LCD-4.3
from WSL2.

| Command | What it does |
| --- | --- |
| `p4 log` | Board log at 115200, read-only. Ctrl+C quits; the app keeps running |
| `p4 repl` | MicroPython REPL (mpremote). Ctrl+] quits; Ctrl+C there stops the app and Ctrl+D restarts it |
| `p4 log --reset` | Restarts the board, then shows the log from the first line of the boot |
| `p4 deploy` | The builder's overlay lane (`tools/deploy_app.py --mode overlay`, see its `README-dev.md`): stops the app, pushes your app checkout over the frozen app and restarts the board. No firmware rebuild |
| `p4 undeploy` | `tools/deploy_app.py --mode overlay-clear`: removes the overlay and restarts the board, so the frozen app runs again |
| `p4 flash <dir>` | Flashes a dist folder (the one holding `flash_args`), the same `esptool write-flash @flash_args` as the builder's README. Prints the chip type and revision first |
| `p4 attach` / `p4 detach` | WSL: hand the board to WSL or back to Windows |

Commands that talk to the board first attach it to WSL with
[usbipd-win](https://github.com/dorssel/usbipd-win) if `/dev/ttyACM0` is missing, so
replugging the board needs nothing extra. While it's attached, Windows has no COM port
for it. Bind it once from an admin prompt (`usbipd bind --hardware-id 1a86:55d3`), and
add yourself to `dialout`.

`p4 deploy` and `p4 undeploy` need a builder checkout. `p4` uses `P4_BUILDER` if set,
otherwise the first that exists of these, next to this devkit:

```
seedsigner-devkit/
micropython/seedsigner-micropython-builder/   or
seedsigner-micropython-builder/
```

The builder pushes the app from its sibling `seedsigner` checkout (and reads `embit` from
its sibling `embit`), which must be on a branch matching the flashed firmware
(`integration/lvgl-mpy`); the builder's `.env` (`SS_APP_DIR`, `SS_EMBIT_DIR`) can point
elsewhere.

The Python that runs esptool, pyserial and mpremote is `P4_PY` if set, otherwise
`~/esptool-venv/bin/python` (the venv the builder's README creates) when it exists,
otherwise `python3`. The script's header lists every environment variable it reads.
