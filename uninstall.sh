#!/usr/bin/env bash
set -Eeuo pipefail

PROJECT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
CONFIG_HOME=${XDG_CONFIG_HOME:-"$HOME/.config"}
STATE_HOME=${XDG_STATE_HOME:-"$HOME/.local/state"}
CACHE_HOME=${XDG_CACHE_HOME:-"$HOME/.cache"}
LIB_DIR="$HOME/.local/lib/waydroid-apple-music"
APP_CONFIG_DIR="$CONFIG_HOME/waydroid-apple-music"
USER_UNIT_DIR="$CONFIG_HOME/systemd/user"
STATE_DIR="$STATE_HOME/waydroid-apple-music"
BACKUP_DIR="$STATE_DIR/backups"
FEATURE_DIR="$STATE_DIR/features"
WAYDROID_CONFIG=/var/lib/waydroid/waydroid.cfg
PIPEWIRE_DROPIN="$CONFIG_HOME/pipewire/pipewire.conf.d/99-waydroid-apple-music-hires.conf"

if ((EUID == 0)); then
    printf 'Do not run this whole uninstaller with sudo. Run ./uninstall.sh as your desktop user.\n' >&2
    exit 1
fi

if [[ ! -d $STATE_DIR ]]; then
    printf 'No install state was found. Nothing was removed.\n'
    exit 0
fi

if [[ -f $STATE_DIR/waydroid-config.json && ! -f $WAYDROID_CONFIG ]]; then
    printf 'Waydroid config is missing. Nothing was removed; install state was kept.\n' >&2
    exit 1
fi
if [[ -f $STATE_DIR/wireplumber-settings.tsv ]]; then
    if ! command -v wpctl >/dev/null 2>&1 || ! wpctl settings >/dev/null 2>&1; then
        printf 'WirePlumber is not available. Nothing was removed; try again after audio starts.\n' >&2
        exit 1
    fi
fi
if [[ -f $FEATURE_DIR/hires-overlay ]] && ! command -v findmnt >/dev/null 2>&1; then
    printf 'findmnt is required to safely restore the Waydroid overlay. Nothing was removed.\n' >&2
    exit 1
fi

TEMP_DIR=$(mktemp -d)
trap 'rm -rf -- "$TEMP_DIR"' EXIT

restore_user_file() {
    local path=$1 label=$2 marker expected_file current_hash expected_hash status
    marker="$BACKUP_DIR/$label.status"
    [[ -f $marker ]] || return
    expected_file="$BACKUP_DIR/$label.installed-sha256"
    if [[ ! -f $path ]]; then
        printf 'Leaving user-removed file alone: %s\n' "$path"
        return
    fi
    if [[ ! -f $expected_file ]]; then
        printf 'Leaving file alone because its install hash is missing: %s\n' "$path"
        return
    fi
    current_hash=$(sha256sum "$path" 2>/dev/null | awk '{print $1}')
    expected_hash=$(<"$expected_file")
    if [[ $current_hash != "$expected_hash" ]]; then
        printf 'Leaving user-changed file alone: %s\n' "$path"
        return
    fi
    status=$(<"$marker")
    if [[ $status == present ]]; then
        install -D -m "$(stat -c '%a' "$BACKUP_DIR/$label")" \
            "$BACKUP_DIR/$label" "$path"
    else
        rm -f -- "$path"
    fi
}

restore_overlay_file() {
    local destination=$1 label=$2 expected_hash=$3 marker status current_hash
    marker="$BACKUP_DIR/$label.status"
    [[ -f $marker ]] || return
    if ! sudo test -f "$destination"; then
        printf 'Leaving externally removed overlay alone: %s\n' "$destination"
        return
    fi
    current_hash=$(sha256sum "$destination" 2>/dev/null | awk '{print $1}')
    if [[ $current_hash != "$expected_hash" ]]; then
        printf 'Leaving externally changed overlay alone: %s\n' "$destination"
        return
    fi
    status=$(<"$marker")
    if [[ $status == present ]]; then
        sudo install -D -m "$(stat -c '%a' "$BACKUP_DIR/$label")" \
            "$BACKUP_DIR/$label" "$destination"
    else
        sudo rm -f -- "$destination"
    fi
}

stop_waydroid_for_overlay() {
    printf 'Stopping Waydroid before restoring its audio overlay...\n'
    waydroid session stop >/dev/null 2>&1 || true
    for _ in {1..40}; do
        if ! waydroid_rootfs_mounted; then
            return
        fi
        sleep 0.25
    done
    sudo systemctl stop waydroid-container.service
    if waydroid_rootfs_mounted; then
        printf 'Waydroid rootfs is still mounted. The overlay was not changed.\n' >&2
        exit 1
    fi
}

waydroid_rootfs_mounted() {
    findmnt -rn -o TARGET | awk '
        $0 == "/var/lib/waydroid/rootfs" ||
        index($0, "/var/lib/waydroid/rootfs/") == 1 { found = 1 }
        END { exit !found }
    '
}

systemctl --user disable --now waydroid-apple-mpris.service >/dev/null 2>&1 || true

waydroid_changed=0
if [[ -f $FEATURE_DIR/hires-overlay ]]; then
    stop_waydroid_for_overlay
    overlay_root=/var/lib/waydroid/overlay/vendor
    sudo rm -f -- \
        "$overlay_root/lib/hw/audio.primary.waydroid.so.waydroid-apple-music.new" \
        "$overlay_root/lib64/hw/audio.primary.waydroid.so.waydroid-apple-music.new" \
        "$overlay_root/etc/audio_policy_configuration.xml.waydroid-apple-music.new"
    restore_overlay_file "$overlay_root/lib/hw/audio.primary.waydroid.so" overlay-lib32 \
        8dfce910cf5ab8d9c09d41931929f9c44d5d7dbfc39209bea43dc485c97aa181
    restore_overlay_file "$overlay_root/lib64/hw/audio.primary.waydroid.so" overlay-lib64 \
        1359f72b9a54a364f334f12e1c1851c78743a0865ec506cf2bfa9a1e2ab3e740
    restore_overlay_file "$overlay_root/etc/audio_policy_configuration.xml" overlay-policy \
        10c5585054a1dd3bc0aa622947f63639b9a3eff68b58a0934d58a5102fd3defa
    waydroid_changed=1
fi

if [[ -f $STATE_DIR/waydroid-config.json && -f $WAYDROID_CONFIG ]]; then
    /usr/bin/python3 "$PROJECT_DIR/scripts/waydroid_config.py" restore \
        --config "$WAYDROID_CONFIG" \
        --output "$TEMP_DIR/waydroid.cfg" \
        --state "$STATE_DIR/waydroid-config.json"
    sudo cp -- "$TEMP_DIR/waydroid.cfg" "$WAYDROID_CONFIG"
    waydroid_changed=1
fi

if ((waydroid_changed)); then
    sudo waydroid upgrade -o
fi

audio_changed=0
if [[ -f $FEATURE_DIR/pipewire-hires ]]; then
    restore_user_file "$PIPEWIRE_DROPIN" pipewire-hires.conf
    audio_changed=1
fi

if [[ -f $STATE_DIR/wireplumber-settings.tsv ]]; then
    while IFS=$'\t' read -r key previous_saved previous_value applied; do
        [[ -n $key ]] || continue
        output=$(wpctl settings "$key" 2>/dev/null || true)
        current_saved=$(sed -n 's/.*Saved: \([^)]*\).*/\1/p' <<<"$output")
        current_saved=${current_saved:-__UNSET__}
        current_value=$(sed -n 's/^Value: \([^[:space:]]*\).*/\1/p' <<<"$output")
        if [[ $current_saved != "$applied" || $current_value != "$applied" ]]; then
            printf 'Leaving user-changed WirePlumber setting alone: %s\n' "$key"
            continue
        fi
        if [[ $previous_saved == __UNSET__ ]]; then
            wpctl settings --delete "$key"
            wpctl settings "$key" "$previous_value"
        else
            wpctl settings --save "$key" "$previous_saved"
            if [[ $previous_value != "$previous_saved" ]]; then
                wpctl settings "$key" "$previous_value"
            fi
        fi
    done <"$STATE_DIR/wireplumber-settings.tsv"
fi

if ((audio_changed)); then
    systemctl --user restart pipewire.service wireplumber.service
fi

restore_user_file "$LIB_DIR/waydroid_apple_mpris.py" bridge.py
restore_user_file "$APP_CONFIG_DIR/bridge.env" bridge.env
restore_user_file "$USER_UNIT_DIR/waydroid-apple-mpris.service" service
systemctl --user daemon-reload

before_enabled=not-found
before_active=inactive
if [[ -f $STATE_DIR/service-before ]]; then
    before_enabled=$(sed -n '1p' "$STATE_DIR/service-before")
    before_active=$(sed -n '2p' "$STATE_DIR/service-before")
fi
if [[ $before_enabled == enabled ]]; then
    systemctl --user enable waydroid-apple-mpris.service >/dev/null
fi
if [[ $before_active == active ]]; then
    systemctl --user start waydroid-apple-mpris.service
fi

case "$STATE_DIR" in
    */waydroid-apple-music) rm -rf -- "$STATE_DIR" ;;
    *) printf 'Refusing to remove unexpected state path: %s\n' "$STATE_DIR" >&2; exit 1 ;;
esac
rm -rf -- "$CACHE_HOME/waydroid-apple-music"
rmdir -- "$LIB_DIR" "$APP_CONFIG_DIR" 2>/dev/null || true

printf 'Removed this project and restored the settings it replaced.\n'
