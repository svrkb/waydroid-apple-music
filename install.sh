#!/usr/bin/env bash
set -Eeuo pipefail

PROJECT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
CONFIG_HOME=${XDG_CONFIG_HOME:-"$HOME/.config"}
STATE_HOME=${XDG_STATE_HOME:-"$HOME/.local/state"}
LIB_DIR="$HOME/.local/lib/waydroid-apple-music"
APP_CONFIG_DIR="$CONFIG_HOME/waydroid-apple-music"
USER_UNIT_DIR="$CONFIG_HOME/systemd/user"
STATE_DIR="$STATE_HOME/waydroid-apple-music"
BACKUP_DIR="$STATE_DIR/backups"
FEATURE_DIR="$STATE_DIR/features"
WAYDROID_CONFIG=/var/lib/waydroid/waydroid.cfg
VENDOR_IMAGE=/var/lib/waydroid/images/vendor.img
PIPEWIRE_DROPIN="$CONFIG_HOME/pipewire/pipewire.conf.d/99-waydroid-apple-music-hires.conf"

ALL=0
ARTWORK=0
ROOT_WORKAROUND=0
HIRES=0
FORCE_VOLUME=0
BLUETOOTH_FIX=0
FOLLOW_OUTPUT=0
AUDIO_WATCHDOG=0

if ((EUID == 0)); then
    printf 'Do not run this whole installer with sudo. Run ./install.sh as your desktop user.\n' >&2
    exit 1
fi

usage() {
    cat <<'EOF'
Usage: ./install.sh [--all]

With no flag, the installer asks before each optional change.
--all turns on every feature.
EOF
}

while (($#)); do
    case "$1" in
        --all) ALL=1 ;;
        -h|--help) usage; exit 0 ;;
        *) printf 'Unknown option: %s\n' "$1" >&2; usage >&2; exit 2 ;;
    esac
    shift
done

ask() {
    local prompt=$1 default=$2 reply
    if [[ ! -t 0 ]]; then
        printf 'Run this installer in a terminal, or use --all.\n' >&2
        exit 2
    fi
    if [[ $default == yes ]]; then
        read -r -p "$prompt [Y/n] " reply || true
        reply=${reply:-y}
    else
        read -r -p "$prompt [y/N] " reply || true
        reply=${reply:-n}
    fi
    [[ $reply == [Yy] || $reply == [Yy][Ee][Ss] ]]
}

if ((ALL)); then
    ARTWORK=1
    ROOT_WORKAROUND=1
    HIRES=1
    FORCE_VOLUME=1
    BLUETOOTH_FIX=1
    FOLLOW_OUTPUT=1
    AUDIO_WATCHDOG=1
else
    ask "Look up album artwork with MusicBrainz and Cover Art Archive?" no && ARTWORK=1
    ask "Apply the tested release-keys workaround for Apple's rooted-device warning?" no && ROOT_WORKAROUND=1
    ask "Install the experimental 32-bit/192 kHz Waydroid audio path?" no && HIRES=1
    ask "Disable Waydroid's safe-volume cap and keep Android music at 15/15?" no && FORCE_VOLUME=1
    ask "Stop automatic Bluetooth phone-call mode for the whole Linux session?" no && BLUETOOTH_FIX=1
    ask "Make movable Linux audio streams follow the current output device?" no && FOLLOW_OUTPUT=1
    ask "Restart Waydroid automatically if its output stream disappears?" no && AUDIO_WATCHDOG=1
fi

if ! command -v waydroid >/dev/null 2>&1; then
    printf 'Waydroid is not installed. Install it first: https://docs.waydro.id/usage/install-on-desktops\n' >&2
    exit 1
fi

packages=()
command -v adb >/dev/null 2>&1 || packages+=(adb)
if ((BLUETOOTH_FIX || FOLLOW_OUTPUT)); then
    command -v wpctl >/dev/null 2>&1 || packages+=(wireplumber)
fi
if ((AUDIO_WATCHDOG)); then
    command -v pw-mon >/dev/null 2>&1 || packages+=(pipewire-bin)
fi
if ((HIRES)); then
    command -v debugfs >/dev/null 2>&1 || packages+=(e2fsprogs)
    command -v findmnt >/dev/null 2>&1 || packages+=(util-linux)
fi
if ! /usr/bin/python3 -c 'import dbus; from gi.repository import GLib' >/dev/null 2>&1; then
    packages+=(python3-dbus python3-gi)
fi

if ((${#packages[@]})); then
    if ! command -v apt-get >/dev/null 2>&1; then
        printf 'Missing commands or Python packages. Install: %s\n' "${packages[*]}" >&2
        exit 1
    fi
    if ((ALL)) || ask "Install the missing Ubuntu packages (${packages[*]})?" yes; then
        sudo apt-get install -y "${packages[@]}"
    else
        printf 'Cannot continue without: %s\n' "${packages[*]}" >&2
        exit 1
    fi
fi

if ((BLUETOOTH_FIX || FOLLOW_OUTPUT)); then
    wp_keys=()
    ((BLUETOOTH_FIX)) && wp_keys+=(bluetooth.autoswitch-to-headset-profile)
    if ((FOLLOW_OUTPUT)); then
        wp_keys+=(linking.follow-default-target linking.allow-moving-streams)
    fi
    for key in "${wp_keys[@]}"; do
        if ! wpctl settings "$key" >/dev/null 2>&1; then
            printf 'WirePlumber 0.5 with the %s setting is required. Project files were not installed.\n' "$key" >&2
            exit 1
        fi
    done
fi

ADB_PATH=$(command -v adb)
mkdir -p "$LIB_DIR" "$APP_CONFIG_DIR" "$USER_UNIT_DIR" "$BACKUP_DIR" "$FEATURE_DIR"
TEMP_DIR=$(mktemp -d)
trap 'rm -rf -- "$TEMP_DIR"' EXIT

if ((HIRES)); then
    if [[ ! -f $VENDOR_IMAGE ]]; then
        printf 'Waydroid vendor image not found: %s\n' "$VENDOR_IMAGE" >&2
        exit 1
    fi
    if ! /usr/bin/python3 "$PROJECT_DIR/scripts/patch_waydroid_hires.py" \
        --vendor-image "$VENDOR_IMAGE" \
        --output "$TEMP_DIR/hires"; then
        if [[ -e $FEATURE_DIR/hires-overlay ]]; then
            printf '\nThe Waydroid image no longer matches the installed high-rate overlay.\n' >&2
            printf 'Run ./uninstall.sh before starting Waydroid. This avoids loading a stale audio overlay.\n' >&2
        fi
        exit 1
    fi
fi

backup_user_file() {
    local path=$1 label=$2 marker
    marker="$BACKUP_DIR/$label.status"
    [[ -e $marker ]] && return
    if [[ -f $path ]]; then
        cp -p -- "$path" "$BACKUP_DIR/$label"
        printf 'present\n' >"$marker"
    else
        printf 'absent\n' >"$marker"
    fi
}

record_installed_hash() {
    local source=$1 label=$2
    sha256sum "$source" | awk '{print $1}' >"$BACKUP_DIR/$label.installed-sha256"
}

backup_overlay_file() {
    local destination=$1 label=$2 marker
    marker="$BACKUP_DIR/$label.status"
    [[ -e $marker ]] && return
    if sudo test -f "$destination"; then
        sudo cp --preserve=mode,timestamps -- "$destination" "$BACKUP_DIR/$label"
        sudo chown "$(id -u):$(id -g)" "$BACKUP_DIR/$label"
        printf 'present\n' >"$marker"
    else
        printf 'absent\n' >"$marker"
    fi
}

save_wp_setting() {
    local key=$1 applied=$2 output previous_saved previous_value settings_file
    settings_file="$STATE_DIR/wireplumber-settings.tsv"
    if ! grep -Fq -- "$key"$'\t' "$settings_file" 2>/dev/null; then
        output=$(wpctl settings "$key")
        previous_saved=$(sed -n 's/.*Saved: \([^)]*\).*/\1/p' <<<"$output")
        previous_saved=${previous_saved:-__UNSET__}
        previous_value=$(sed -n 's/^Value: \([^[:space:]]*\).*/\1/p' <<<"$output")
        if [[ -z $previous_value ]]; then
            printf 'Could not read WirePlumber setting: %s\n' "$key" >&2
            exit 1
        fi
        printf '%s\t%s\t%s\t%s\n' \
            "$key" "$previous_saved" "$previous_value" "$applied" \
            >>"$settings_file"
    fi
    wpctl settings --save "$key" "$applied"
}

stop_waydroid_for_overlay() {
    printf 'Stopping Waydroid before changing its audio overlay...\n'
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

backup_user_file "$LIB_DIR/waydroid_apple_mpris.py" bridge.py
backup_user_file "$APP_CONFIG_DIR/bridge.env" bridge.env
backup_user_file "$USER_UNIT_DIR/waydroid-apple-mpris.service" service
if [[ ! -e $STATE_DIR/service-before ]]; then
    before_enabled=$(systemctl --user is-enabled waydroid-apple-mpris.service 2>/dev/null || true)
    before_active=$(systemctl --user is-active waydroid-apple-mpris.service 2>/dev/null || true)
    printf '%s\n%s\n' "${before_enabled:-not-found}" "${before_active:-inactive}" \
        >"$STATE_DIR/service-before"
fi

record_installed_hash "$PROJECT_DIR/src/waydroid_apple_mpris.py" bridge.py
record_installed_hash "$PROJECT_DIR/systemd/waydroid-apple-mpris.service" service
install -m 0755 "$PROJECT_DIR/src/waydroid_apple_mpris.py" "$LIB_DIR/waydroid_apple_mpris.py"
install -m 0644 "$PROJECT_DIR/systemd/waydroid-apple-mpris.service" "$USER_UNIT_DIR/waydroid-apple-mpris.service"

cat >"$TEMP_DIR/bridge.env" <<EOF
WAYDROID_APPLE_MPRIS_CATALOG_ARTWORK=$ARTWORK
WAYDROID_APPLE_MPRIS_FORCE_VOLUME=$FORCE_VOLUME
WAYDROID_APPLE_MPRIS_AUDIO_WATCHDOG=$AUDIO_WATCHDOG
WAYDROID_APPLE_MPRIS_WATCHDOG_GRACE=1.0
WAYDROID_APPLE_MPRIS_ADB=$ADB_PATH
EOF
record_installed_hash "$TEMP_DIR/bridge.env" bridge.env
install -m 0600 "$TEMP_DIR/bridge.env" "$APP_CONFIG_DIR/bridge.env"
touch "$FEATURE_DIR/bridge"

waydroid_changed=0
config_args=()
if ((ROOT_WORKAROUND)); then
    config_args+=(--root-workaround)
fi
if ((HIRES)); then
    config_args+=(--hires)
fi

if ((${#config_args[@]})); then
    if [[ ! -f $WAYDROID_CONFIG ]]; then
        printf 'Waydroid config not found: %s\n' "$WAYDROID_CONFIG" >&2
        exit 1
    fi
    /usr/bin/python3 "$PROJECT_DIR/scripts/waydroid_config.py" apply \
        --config "$WAYDROID_CONFIG" \
        --output "$TEMP_DIR/waydroid.cfg" \
        --state "$STATE_DIR/waydroid-config.json" \
        "${config_args[@]}"
    sudo cp -- "$TEMP_DIR/waydroid.cfg" "$WAYDROID_CONFIG"
    if ((ROOT_WORKAROUND)); then
        touch "$FEATURE_DIR/root-workaround"
    fi
    waydroid_changed=1
fi

if ((HIRES)); then
    stop_waydroid_for_overlay
    overlay_root=/var/lib/waydroid/overlay/vendor
    overlay32="$overlay_root/lib/hw/audio.primary.waydroid.so"
    overlay64="$overlay_root/lib64/hw/audio.primary.waydroid.so"
    overlay_policy="$overlay_root/etc/audio_policy_configuration.xml"
    backup_overlay_file "$overlay32" overlay-lib32
    backup_overlay_file "$overlay64" overlay-lib64
    backup_overlay_file "$overlay_policy" overlay-policy
    touch "$FEATURE_DIR/hires-overlay"

    # Stage each file beside its destination. The final renames are atomic, so
    # an interrupted copy cannot leave a truncated audio HAL in the overlay.
    sudo install -D -m 0644 \
        "$TEMP_DIR/hires/vendor/lib/hw/audio.primary.waydroid.so" \
        "$overlay32.waydroid-apple-music.new"
    sudo install -D -m 0644 \
        "$TEMP_DIR/hires/vendor/lib64/hw/audio.primary.waydroid.so" \
        "$overlay64.waydroid-apple-music.new"
    sudo install -D -m 0644 \
        "$TEMP_DIR/hires/vendor/etc/audio_policy_configuration.xml" \
        "$overlay_policy.waydroid-apple-music.new"
    sudo mv -f -- "$overlay32.waydroid-apple-music.new" "$overlay32"
    sudo mv -f -- "$overlay64.waydroid-apple-music.new" "$overlay64"
    sudo mv -f -- "$overlay_policy.waydroid-apple-music.new" "$overlay_policy"
    backup_user_file "$PIPEWIRE_DROPIN" pipewire-hires.conf
    record_installed_hash \
        "$PROJECT_DIR/config/10-waydroid-shared-hires.conf" pipewire-hires.conf
    touch "$FEATURE_DIR/pipewire-hires"
    install -D -m 0644 \
        "$PROJECT_DIR/config/10-waydroid-shared-hires.conf" \
        "$PIPEWIRE_DROPIN"
    waydroid_changed=1
fi

if ((waydroid_changed)); then
    sudo waydroid upgrade -o
fi

if ((HIRES)); then
    systemctl --user restart pipewire.service wireplumber.service
fi

if ((BLUETOOTH_FIX)); then
    save_wp_setting bluetooth.autoswitch-to-headset-profile false
    touch "$FEATURE_DIR/bluetooth-fix"
fi
if ((FOLLOW_OUTPUT)); then
    save_wp_setting linking.follow-default-target true
    save_wp_setting linking.allow-moving-streams true
    touch "$FEATURE_DIR/follow-output"
fi

systemctl --user daemon-reload
systemctl --user enable waydroid-apple-mpris.service
# Restart so a re-run picks up new bridge code or bridge.env settings; a
# plain enable --now would leave the old process running.
systemctl --user restart waydroid-apple-mpris.service

printf '\nInstalled. Start Apple Music in Waydroid and accept the one-time ADB prompt.\n'
printf 'Run ./verify.sh to check the setup. Run ./uninstall.sh to undo it.\n'
