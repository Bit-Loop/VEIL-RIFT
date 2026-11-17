#!/usr/bin/env bash
# Prefer a high-quality space wallpaper from NASA APOD; fall back to Bing UHD.
set -euo pipefail

APOD_DIR="${HOME}/Pictures/nasa-apod"
BING_DIR="${HOME}/Pictures/bing-uhd"
STATE_DIR="${HOME}/Pictures/space-wallpaper"
SAVED_DIR="${HOME}/Pictures/wallpaper-mode"
STATE_LINK="${STATE_DIR}/current.jpg"
SAVED_META="${SAVED_DIR}/current-static.json"
MODE_FILE="${HOME}/.config/wallpaper-mode/state"
APOD_SCAN_STAMP="${STATE_DIR}/apod-scan-$(date '+%F').done"
LOG_FILE="${HOME}/.local/state/nasa-apod-wallpaper.log"
LOCK_FILE="${HOME}/.local/state/space-wallpaper.lock"

NASA_API_KEY="${NASA_API_KEY:-DEMO_KEY}"
APOD_LOOKBACK_DAYS="${APOD_LOOKBACK_DAYS:-10}"

MIN_WIDTH="${MIN_WIDTH:-2560}"
MIN_HEIGHT="${MIN_HEIGHT:-1440}"
MIN_FILE_BYTES="${MIN_FILE_BYTES:-1000000}"
MIN_BYTES_PER_MEGAPIXEL="${MIN_BYTES_PER_MEGAPIXEL:-250000}"
MIN_ASPECT_X100="${MIN_ASPECT_X100:-90}"
MAX_ASPECT_X100="${MAX_ASPECT_X100:-300}"

SELECTED_PATH=""
SELECTED_SOURCE=""
SELECTED_DATE=""
SELECTED_TITLE=""

mkdir -p "$APOD_DIR" "$BING_DIR" "$STATE_DIR" "$SAVED_DIR" "$(dirname "$LOG_FILE")"

log() { printf '%s %s\n' "$(date '+%Y-%m-%dT%H:%M:%S%z')" "$*" >> "$LOG_FILE"; }

if [[ "${WALLPAPER_MODE_FORCE_STATIC:-0}" != "1" && -r "$MODE_FILE" ]]; then
    current_mode="$(tr -d '[:space:]' < "$MODE_FILE")"
    if [[ -n "$current_mode" && "$current_mode" != "static" ]]; then
        log "SKIP: live wallpaper mode (${current_mode}) is active"
        exit 0
    fi
fi

exec 9>"$LOCK_FILE"
if ! flock -n 9; then
    log "SKIP: another wallpaper update is already running"
    exit 0
fi

cleanup_cache() {
    find "$APOD_DIR" "$BING_DIR" -maxdepth 1 -type f -name '*.part.*' -mtime +1 -delete
    find "$STATE_DIR" -maxdepth 1 -type f -name 'apod-scan-*.done' -mtime +14 -delete
}

extension_for_url() {
    local url_path ext
    url_path="${1%%\?*}"
    ext="${url_path##*.}"
    ext="${ext,,}"
    case "$ext" in
        jpg|jpeg|png) printf '%s\n' "$ext" ;;
        *) printf 'jpg\n' ;;
    esac
}

slugify() {
    local raw slug
    raw="$1"
    slug="$(printf '%s' "$raw" \
        | tr '[:upper:]' '[:lower:]' \
        | sed -E 's/[^a-z0-9._-]+/-/g; s/-+/-/g; s/^-//; s/-$//' \
        | cut -c1-96)"
    if [[ -z "$slug" ]]; then
        printf 'wallpaper\n'
    else
        printf '%s\n' "$slug"
    fi
}

download_image() {
    local url="$1"
    local dest="$2"
    local label="$3"
    local tmp

    if [[ -s "$dest" ]]; then
        return 0
    fi

    tmp="${dest}.part.$$"
    rm -f "$tmp"

    if ! curl -fsSL --retry 3 --connect-timeout 15 --max-time 180 -o "$tmp" "$url"; then
        rm -f "$tmp"
        log "ERROR: download failed for ${label}: ${url}"
        return 1
    fi

    mv "$tmp" "$dest"
}

validate_wallpaper() {
    local path="$1"
    local label="$2"
    local width height bytes pixels bytes_per_mp aspect

    if ! read -r width height < <(identify -quiet -format '%w %h\n' "$path" 2>/dev/null); then
        log "REJECT: ${label}: not a readable image"
        return 1
    fi

    bytes="$(stat -c '%s' "$path")"
    pixels=$((width * height))
    bytes_per_mp=$((bytes * 1000000 / pixels))
    aspect=$((width * 100 / height))

    if ((width < MIN_WIDTH || height < MIN_HEIGHT)); then
        log "REJECT: ${label}: ${width}x${height} below ${MIN_WIDTH}x${MIN_HEIGHT}"
        return 1
    fi

    if ((aspect < MIN_ASPECT_X100 || aspect > MAX_ASPECT_X100)); then
        log "REJECT: ${label}: aspect ${aspect}/100 outside ${MIN_ASPECT_X100}-${MAX_ASPECT_X100}/100"
        return 1
    fi

    if ((bytes < MIN_FILE_BYTES)); then
        log "REJECT: ${label}: ${bytes} bytes below ${MIN_FILE_BYTES}"
        return 1
    fi

    if ((bytes_per_mp < MIN_BYTES_PER_MEGAPIXEL)); then
        log "REJECT: ${label}: ${bytes_per_mp} bytes/MP below ${MIN_BYTES_PER_MEGAPIXEL}"
        return 1
    fi

    log "ACCEPT: ${label}: ${width}x${height}, ${bytes} bytes, ${bytes_per_mp} bytes/MP"
    return 0
}

select_image() {
    SELECTED_PATH="$1"
    SELECTED_SOURCE="$2"
    SELECTED_DATE="$3"
    SELECTED_TITLE="$4"
}

try_apod_date() {
    local date_str="$1"
    local meta media_type title url ext dest label tmp http_code

    tmp="$(mktemp)"
    if ! http_code="$(curl -sSL --retry 0 --connect-timeout 10 --max-time 30 -w '%{http_code}' -o "$tmp" "https://api.nasa.gov/planetary/apod?api_key=${NASA_API_KEY}&thumbs=true&date=${date_str}")"; then
        rm -f "$tmp"
        log "ERROR: APOD metadata request failed for ${date_str}"
        return 3
    fi

    if [[ "$http_code" == "429" ]]; then
        rm -f "$tmp"
        log "WARN: APOD rate limit hit for ${date_str}; falling back"
        return 2
    fi

    if [[ ! "$http_code" =~ ^2 ]]; then
        rm -f "$tmp"
        log "ERROR: APOD metadata request for ${date_str} returned HTTP ${http_code}"
        return 3
    fi

    meta="$(<"$tmp")"
    rm -f "$tmp"

    if ! jq -e 'type == "object"' >/dev/null 2>&1 <<<"$meta"; then
        log "ERROR: APOD metadata was not valid JSON for ${date_str}"
        return 3
    fi

    media_type="$(jq -r '.media_type // empty' <<<"$meta")"
    title="$(jq -r '.title // "untitled"' <<<"$meta")"

    if [[ "$media_type" != "image" ]]; then
        log "SKIP: APOD ${date_str} '${title}': media_type=${media_type}"
        return 1
    fi

    url="$(jq -r '.hdurl // .url // empty' <<<"$meta")"
    if [[ -z "$url" || "$url" == "null" ]]; then
        log "SKIP: APOD ${date_str} '${title}': no image URL"
        return 1
    fi

    ext="$(extension_for_url "$url")"
    dest="${APOD_DIR}/${date_str}.${ext}"
    label="APOD ${date_str} '${title}'"

    download_image "$url" "$dest" "$label" || return 3
    validate_wallpaper "$dest" "$label" || return 1

    select_image "$dest" "NASA APOD" "$date_str" "$title"
}

try_apod() {
    local offset date_str status network_failures=0

    if [[ -e "$APOD_SCAN_STAMP" && "${WALLPAPER_MODE_FORCE_REFRESH:-0}" != "1" ]]; then
        log "SKIP: APOD already scanned today"
        return 1
    fi

    for offset in $(seq 0 "$APOD_LOOKBACK_DAYS"); do
        date_str="$(date -d "-${offset} day" '+%F')"
        if try_apod_date "$date_str"; then
            : > "$APOD_SCAN_STAMP"
            return 0
        else
            status=$?
            if ((status == 2)); then
                : > "$APOD_SCAN_STAMP"
                return 1
            elif ((status == 3)); then
                network_failures=$((network_failures + 1))
                if ((network_failures >= 2)); then
                    log "WARN: APOD network failed twice in a row; using fallback provider"
                    : > "$APOD_SCAN_STAMP"
                    return 1
                fi
            else
                network_failures=0
            fi
        fi
    done

    : > "$APOD_SCAN_STAMP"
    return 1
}

try_cached_apod() {
    local current path

    if [[ -e "${APOD_DIR}/current.jpg" ]]; then
        current="$(readlink -f "${APOD_DIR}/current.jpg")"
        if validate_wallpaper "$current" "cached APOD current.jpg"; then
            select_image "$current" "NASA APOD" "cached" "$(basename "$current")"
            return 0
        fi
    fi

    while IFS= read -r path; do
        if validate_wallpaper "$path" "cached APOD $(basename "$path")"; then
            select_image "$path" "NASA APOD" "cached" "$(basename "$path")"
            return 0
        fi
    done < <(find "$APOD_DIR" -maxdepth 1 -type f \( -iname '*.jpg' -o -iname '*.jpeg' -o -iname '*.png' \) | sort -r)

    return 1
}

try_bing() {
    local meta urlbase date_str title url dest label

    if ! meta="$(curl -fsSL --retry 2 --connect-timeout 10 --max-time 30 'https://www.bing.com/HPImageArchive.aspx?format=js&idx=0&n=1&mkt=en-US')"; then
        log "ERROR: Bing API request failed"
        return 1
    fi

    if ! jq -e 'type == "object" and (.images | type == "array") and ((.images | length) > 0)' >/dev/null 2>&1 <<<"$meta"; then
        log "ERROR: Bing API response was not valid JSON"
        return 1
    fi

    urlbase="$(jq -r '.images[0].urlbase // empty' <<<"$meta")"
    date_str="$(jq -r '.images[0].startdate // empty' <<<"$meta")"
    title="$(jq -r '.images[0].title // "untitled"' <<<"$meta")"

    if [[ -z "$urlbase" || -z "$date_str" ]]; then
        log "ERROR: Bing API response did not include a usable image"
        return 1
    fi

    url="https://www.bing.com${urlbase}_UHD.jpg"
    dest="${BING_DIR}/${date_str}.jpg"
    label="Bing ${date_str} '${title}'"

    download_image "$url" "$dest" "$label" || return 1
    validate_wallpaper "$dest" "$label" || return 1

    select_image "$dest" "Bing UHD" "$date_str" "$title"
}

try_current() {
    local current

    if [[ ! -e "$STATE_LINK" ]]; then
        return 1
    fi

    current="$(readlink -f "$STATE_LINK")"
    validate_wallpaper "$current" "existing current wallpaper" || return 1
    select_image "$current" "existing current" "cached" "$(basename "$current")"
}

set_wallpaper() {
    export DISPLAY="${DISPLAY:-:0}"
    export XAUTHORITY="${XAUTHORITY:-$HOME/.Xauthority}"

    ln -sfn "$SELECTED_PATH" "$STATE_LINK"

    case "$SELECTED_SOURCE" in
        "NASA APOD")
            ln -sfn "$SELECTED_PATH" "${APOD_DIR}/current.jpg"
            ;;
        "Bing UHD")
            ln -sfn "$SELECTED_PATH" "${BING_DIR}/current.jpg"
            ;;
    esac

    if command -v feh >/dev/null; then
        feh --bg-fill "$STATE_LINK"
    elif command -v swaybg >/dev/null && [[ "${XDG_SESSION_TYPE:-}" == "wayland" ]]; then
        pkill -x swaybg 2>/dev/null || true
        swaybg -i "$STATE_LINK" -m fill &
    else
        log "ERROR: no supported wallpaper setter found"
        return 1
    fi
}

save_selected_wallpaper() {
    local selected_real ext clean_title source_slug title_slug date_slug dest current_link

    selected_real="$(readlink -f "$SELECTED_PATH")"
    ext="${selected_real##*.}"
    ext="${ext,,}"
    case "$ext" in
        jpg|jpeg|png) ;;
        *) ext="jpg" ;;
    esac

    clean_title="$SELECTED_TITLE"
    clean_title="${clean_title%.jpg}"
    clean_title="${clean_title%.JPG}"
    clean_title="${clean_title%.jpeg}"
    clean_title="${clean_title%.JPEG}"
    clean_title="${clean_title%.png}"
    clean_title="${clean_title%.PNG}"

    source_slug="$(slugify "$SELECTED_SOURCE")"
    title_slug="$(slugify "$clean_title")"
    date_slug="$(slugify "$SELECTED_DATE")"
    dest="${SAVED_DIR}/${date_slug}-${source_slug}-${title_slug}.${ext}"

    cp --reflink=auto -f "$selected_real" "$dest"
    rm -f "${SAVED_DIR}"/current-static.*
    current_link="${SAVED_DIR}/current-static.${ext}"
    ln -sfn "$dest" "$current_link"

    jq -n \
        --arg source "$SELECTED_SOURCE" \
        --arg date "$SELECTED_DATE" \
        --arg title "$SELECTED_TITLE" \
        --arg original_path "$selected_real" \
        --arg saved_path "$dest" \
        --arg current_path "$current_link" \
        --arg updated_at "$(date '+%s')" \
        '{
            source: $source,
            date: $date,
            title: $title,
            original_path: $original_path,
            saved_path: $saved_path,
            current_path: $current_path,
            updated_at: ($updated_at | tonumber)
        }' > "$SAVED_META"
}

if ! try_apod && ! try_bing && ! try_cached_apod && ! try_current; then
    log "ERROR: no acceptable wallpaper candidate found"
    exit 1
fi

set_wallpaper
save_selected_wallpaper
cleanup_cache

log "OK: ${SELECTED_SOURCE}: ${SELECTED_DATE} '${SELECTED_TITLE}' -> ${SELECTED_PATH}"
