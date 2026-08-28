#!/usr/bin/env bash
set -u

PROJECT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
CONFIG_HOME=${XDG_CONFIG_HOME:-"$HOME/.config"}
STATE_HOME=${XDG_STATE_HOME:-"$HOME/.local/state"}
STATE_DIR="$STATE_HOME/waydroid-apple-music"
FEATURE_DIR="$STATE_DIR/features"
ENV_FILE="$CONFIG_HOME/waydroid-apple-music/bridge.env"
failures=0
temp_dir=
trap '[[ -z ${temp_dir:-} ]] || rm -rf -- "$temp_dir"' EXIT

ok() { printf 'OK: %s\n' "$*"; }
warn() { printf 'WARN: %s\n' "$*"; }
fail() { printf 'FAIL: %s\n' "$*"; failures=$((failures + 1)); }

if systemctl --user is-active --quiet waydroid-apple-mpris.service; then
    ok "media-control bridge is running"
else
    fail "media-control bridge is not running"
fi

if [[ -f $HOME/.local/lib/waydroid-apple-music/waydroid_apple_mpris.py ]]; then
    ok "bridge code is installed"
else
    fail "bridge code is missing"
fi

if [[ -f $ENV_FILE ]]; then
    adb_path=$(sed -n 's/^WAYDROID_APPLE_MPRIS_ADB=//p' "$ENV_FILE" | tail -n 1)
else
    adb_path=$(command -v adb 2>/dev/null || true)
    warn "bridge config is missing"
fi

if [[ -n ${adb_path:-} && -x $adb_path ]]; then
    ok "adb is available at $adb_path"
else
    fail "adb is missing"
fi

if [[ -f $FEATURE_DIR/root-workaround ]]; then
    cfg=/var/lib/waydroid/waydroid.cfg
    for key in ro.build.tags ro.product.build.tags ro.vendor.build.tags ro.odm.build.tags; do
        if grep -Eq "^[[:space:]]*${key//./\\.}[[:space:]]*=[[:space:]]*release-keys[[:space:]]*$" "$cfg"; then
            ok "$key is release-keys"
        else
            fail "$key is not release-keys"
        fi
    done
    if grep -Eq '^[[:space:]]*ro\.build\.selinux[[:space:]]*=[[:space:]]*1[[:space:]]*$' "$cfg"; then
        ok "ro.build.selinux is 1"
    else
        fail "ro.build.selinux workaround is missing"
    fi
fi

if [[ -f $FEATURE_DIR/hires-overlay ]]; then
    expected32=8dfce910cf5ab8d9c09d41931929f9c44d5d7dbfc39209bea43dc485c97aa181
    expected64=1359f72b9a54a364f334f12e1c1851c78743a0865ec506cf2bfa9a1e2ab3e740
    expected_policy=10c5585054a1dd3bc0aa622947f63639b9a3eff68b58a0934d58a5102fd3defa
    actual32=$(sha256sum /var/lib/waydroid/overlay/vendor/lib/hw/audio.primary.waydroid.so 2>/dev/null | awk '{print $1}')
    actual64=$(sha256sum /var/lib/waydroid/overlay/vendor/lib64/hw/audio.primary.waydroid.so 2>/dev/null | awk '{print $1}')
    actual_policy=$(sha256sum /var/lib/waydroid/overlay/vendor/etc/audio_policy_configuration.xml 2>/dev/null | awk '{print $1}')
    if [[ $actual32 == "$expected32" ]]; then
        ok "32-bit audio overlay matches"
    else
        fail "32-bit audio overlay does not match"
    fi
    if [[ $actual64 == "$expected64" ]]; then
        ok "64-bit audio overlay matches"
    else
        fail "64-bit audio overlay does not match"
    fi
    if [[ $actual_policy == "$expected_policy" ]]; then
        ok "audio policy overlay matches"
    else
        fail "audio policy overlay does not match"
    fi

    temp_dir=$(mktemp -d)
    if /usr/bin/python3 "$PROJECT_DIR/scripts/patch_waydroid_hires.py" \
        --vendor-image /var/lib/waydroid/images/vendor.img \
        --output "$temp_dir" >/dev/null; then
        ok "base Waydroid image still matches this overlay"
    else
        fail "base Waydroid image changed; uninstall before starting Waydroid"
    fi
    rm -rf -- "$temp_dir"
    temp_dir=
fi

if [[ -f $FEATURE_DIR/pipewire-hires ]]; then
    dropin="$CONFIG_HOME/pipewire/pipewire.conf.d/99-waydroid-apple-music-hires.conf"
    if [[ -f $dropin ]] && grep -q 'default.clock.rate = 192000' "$dropin"; then
        ok "PipeWire drop-in requests a shared 192 kHz graph"
    else
        fail "192 kHz PipeWire drop-in is missing"
    fi
fi

if [[ -f $FEATURE_DIR/bluetooth-fix ]] && command -v wpctl >/dev/null 2>&1; then
    if wpctl settings bluetooth.autoswitch-to-headset-profile 2>/dev/null | grep -q 'Value: false'; then
        ok "Bluetooth call-mode autoswitch is off"
    else
        fail "Bluetooth call-mode autoswitch is not off"
    fi
fi

if [[ -f $FEATURE_DIR/follow-output ]] && command -v wpctl >/dev/null 2>&1; then
    for key in linking.follow-default-target linking.allow-moving-streams; do
        if wpctl settings "$key" 2>/dev/null | grep -q 'Value: true'; then
            ok "$key is on"
        else
            fail "$key is not on"
        fi
    done
fi

if waydroid status 2>/dev/null | grep -q 'Session:[[:space:]]*RUNNING'; then
    lease=/var/lib/misc/dnsmasq.waydroid0.leases
    ip=$(awk 'NF >= 3 {value=$3} END {print value}' "$lease" 2>/dev/null)
    if [[ -n ${ip:-} && -n ${adb_path:-} ]]; then
        timeout 5s "$adb_path" connect "$ip:5555" >/dev/null 2>&1 || true
        if [[ $(timeout 5s "$adb_path" -s "$ip:5555" get-state 2>/dev/null) == device ]]; then
            ok "local ADB connection is authorized"
            if timeout 8s "$adb_path" -s "$ip:5555" shell pm path com.apple.android.music 2>/dev/null | grep -q '^package:'; then
                ok "Apple Music is installed in Waydroid"
            else
                warn "Apple Music was not found in Waydroid"
            fi
        else
            warn "ADB is waiting for approval inside Waydroid"
        fi
    else
        warn "Waydroid is running, but its ADB address was not found"
    fi
else
    warn "Waydroid is stopped; start Apple Music to check ADB and the app"
fi

exit "$failures"
