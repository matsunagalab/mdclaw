# Internal runtime selection shared by the launcher, setup and doctor.
# Source this file after setting PKG_ROOT; it performs no work on import.

_mdclaw_conda_exists() {
    command -v conda >/dev/null 2>&1 \
        && conda env list 2>/dev/null | awk '{print $1}' | grep -qx "$CONDA_ENV_NAME"
}

_mdclaw_pixi_exists() {
    command -v pixi >/dev/null 2>&1 \
        && [ -f "$PKG_ROOT/pixi.toml" ] \
        && [ -d "$PKG_ROOT/.pixi/envs/default" ]
}

mdclaw_select_runtime() {
    # "container" is used only by setup's existing version/force-pull options.
    # An explicit MDCLAW_RUNTIME always wins, including pixi and conda.
    local mode="${1:-auto}"
    CONDA_ENV_NAME="${MDCLAW_CONDA_ENV:-mdclaw}"
    DEFAULT_IMAGE_TAG="$(sed -n 's/.*"version"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' \
        "$PKG_ROOT/.claude-plugin/plugin.json" 2>/dev/null | head -n 1)" || DEFAULT_IMAGE_TAG=""
    DOCKER_IMAGE="${MDCLAW_DOCKER_IMAGE:-ghcr.io/matsunagalab/mdclaw:${DEFAULT_IMAGE_TAG:-latest}}"
    # Keep an explicit path even if missing: never silently use another SIF.
    if [ -n "${MDCLAW_SIF:-}" ]; then
        SIF="$MDCLAW_SIF"
    elif [ -n "${CLAUDE_PLUGIN_DATA:-}" ] && [ -f "$CLAUDE_PLUGIN_DATA/mdclaw.sif" ]; then
        SIF="$CLAUDE_PLUGIN_DATA/mdclaw.sif"
    elif [ -f "$PKG_ROOT/mdclaw.sif" ]; then
        SIF="$PKG_ROOT/mdclaw.sif"
    else
        SIF="${CLAUDE_PLUGIN_DATA:-$PKG_ROOT}/mdclaw.sif"
    fi
    LOCAL_MDCLAW="$(command -v mdclaw 2>/dev/null || true)"
    if [ -n "$LOCAL_MDCLAW" ] && [ "$LOCAL_MDCLAW" -ef "$PKG_ROOT/bin/mdclaw" ]; then
        LOCAL_MDCLAW=""
    fi
    RUNTIME="${MDCLAW_RUNTIME:-}"
    case "$RUNTIME" in
        ""|pixi|conda|singularity|apptainer|docker) ;;
        *)
            echo "Error: unknown MDCLAW_RUNTIME='$RUNTIME'." >&2
            echo "Next: choose pixi, conda, singularity, apptainer or docker." >&2
            return 1 ;;
    esac
    if [ -z "$RUNTIME" ]; then
        if [ "$mode" != container ] && _mdclaw_pixi_exists; then
            RUNTIME=pixi
        elif [ "$mode" != container ] && _mdclaw_conda_exists; then
            RUNTIME=conda
        elif command -v singularity >/dev/null 2>&1; then
            RUNTIME=singularity
        elif command -v apptainer >/dev/null 2>&1; then
            RUNTIME=apptainer
        elif command -v docker >/dev/null 2>&1; then
            RUNTIME=docker
        elif [ "$mode" != container ] && [ -n "$LOCAL_MDCLAW" ]; then
            RUNTIME=local
        else
            echo "Error: no MDClaw runtime found." >&2
            echo "Next: run pixi install in $PKG_ROOT, or install Singularity/Apptainer for a shared SIF." >&2
            return 1
        fi
    fi
    CONTAINER_CMD="$RUNTIME"
}

mdclaw_require_runtime() {
    # Setup may download a missing SIF. Execution and diagnosis never do.
    local purpose="${1:-run}"
    case "$RUNTIME" in
        pixi)
            if ! _mdclaw_pixi_exists; then
                echo "Error: selected pixi environment is not installed." >&2
                echo "Next: install Pixi, then run pixi install in $PKG_ROOT." >&2
                return 1
            fi ;;
        conda)
            if ! _mdclaw_conda_exists; then
                echo "Error: selected conda environment '$CONDA_ENV_NAME' is not installed." >&2
                echo "Next: conda env create -n '$CONDA_ENV_NAME' -f '$PKG_ROOT/environment.yml'" >&2
                return 1
            fi ;;
        singularity|apptainer)
            if ! command -v "$RUNTIME" >/dev/null 2>&1; then
                echo "Error: selected $RUNTIME command is not installed." >&2
                echo "Next: load/install $RUNTIME on this host, or select another installed runtime." >&2
                return 1
            fi
            if [ "$purpose" != setup ] && { [ ! -f "$SIF" ] || [ ! -r "$SIF" ]; }; then
                echo "Error: selected SIF is missing or unreadable: $SIF" >&2
                echo "Next: check MDCLAW_SIF or run $PKG_ROOT/scripts/setup-container.sh." >&2
                return 1
            fi ;;
        docker)
            if ! command -v docker >/dev/null 2>&1 || ! docker info >/dev/null 2>&1; then
                echo "Error: selected Docker runtime is unavailable." >&2
                echo "Next: install/start Docker and verify docker info." >&2
                return 1
            fi
            if [ "$purpose" != setup ] && ! docker image inspect "$DOCKER_IMAGE" >/dev/null 2>&1; then
                echo "Error: selected Docker image is not installed: $DOCKER_IMAGE" >&2
                echo "Next: run $PKG_ROOT/scripts/setup-container.sh." >&2
                return 1
            fi ;;
    esac
}

mdclaw_describe_runtime() {
    local selection=auto
    [ -n "${MDCLAW_RUNTIME:-}" ] && selection=explicit
    echo "Runtime: $RUNTIME ($selection)"
    case "$RUNTIME" in
        pixi) echo "Environment: $PKG_ROOT/.pixi/envs/default" ;;
        conda) echo "Environment: $CONDA_ENV_NAME" ;;
        singularity|apptainer) echo "Image: $SIF" ;;
        docker) echo "Image: $DOCKER_IMAGE" ;;
        local) echo "Executable: $LOCAL_MDCLAW" ;;
    esac
}

# Directories to bind for the absolute paths found in the arguments (a
# job_dir, a study_dir, a reference structure inside a JSON argument): the
# deepest existing directory of each path, one per line, minus the working
# directory, the package root, $HOME (bound by default) and system prefixes.
# Without this a job_dir outside the working directory is invisible inside
# the container and the tools report a job that exists as "no progress.json".
_mdclaw_arg_bind_dirs() {
    local cwd="$1" pkg="$2"
    shift 2
    local arg path dir seen="|" skip
    for arg in "$@"; do
        while IFS= read -r path; do
            [ -n "$path" ] || continue
            dir="$path"
            while [ ! -d "$dir" ] && [ "$dir" != "/" ]; do
                dir="$(dirname "$dir")"
            done
            { [ -d "$dir" ] && [ "$dir" != "/" ]; } || continue
            case "$dir" in
                /proc|/proc/*|/sys|/sys/*|/dev|/dev/*|/usr|/usr/*|/etc|/etc/*|/bin|/bin/*|/sbin|/sbin/*|/lib|/lib/*|/lib64|/lib64/*|/opt|/opt/*|/tmp|/tmp/*|/var|/var/*|/run|/run/*|/root|/root/*) continue ;;
                "$cwd"|"$cwd"/*|"$pkg"|"$pkg"/*) continue ;;
            esac
            if [ -n "${HOME:-}" ]; then
                case "$dir" in "$HOME"|"$HOME"/*) continue ;; esac
            fi
            case "$seen" in *"|$dir|"*) continue ;; esac
            seen="${seen}${dir}|"
        done < <(printf '%s\n' "$arg" | grep -oE '/[^]"'"'"'[:space:],}]+' || true)
    done
    # Keep the minimal set: a directory whose ancestor is also listed is
    # covered by that ancestor's bind (nested binds are not needed).
    local candidate other
    printf '%s\n' "${seen//|/$'\n'}" | sed '/^$/d' | while IFS= read -r candidate; do
        skip=""
        other="$(dirname "$candidate")"
        while [ "$other" != "/" ]; do
            case "$seen" in *"|$other|"*) skip=1; break ;; esac
            other="$(dirname "$other")"
        done
        [ -n "$skip" ] || printf '%s\n' "$candidate"
    done
}


# Exec in the launcher; doctor calls this in a subshell to continue checking.
mdclaw_runtime_exec() {
case "$RUNTIME" in
    pixi)
        # Run mdclaw inside the checkout's pixi env (created with
        # `pixi install` from pixi.toml + pixi.lock). --frozen uses the env
        # as installed without re-solving; --no-install prevents downloads; `pixi run` performs the full
        # activation (PATH, AMBERHOME, ...) like `conda run` does.
        exec pixi run --frozen --no-install --manifest-path "$PKG_ROOT/pixi.toml" \
            "$@"
        ;;
    conda)
        # Run mdclaw entirely inside the conda env. The env must contain
        # mdclaw (-e .) plus its scientific dependencies. No mixing with
        # the host Python.
        exec conda run --no-capture-output -n "$CONDA_ENV_NAME" \
            "$@"
        ;;
    singularity|apptainer)
        # In a user namespace the kernel ignores the setuid bit on
        # starter-suid and fusermount3, so the SIF cannot be mounted and every
        # call re-extracts it instead. See docs/developer/container.md.
        # MDCLAW_UID_MAP_FILE is a test seam, not a supported configuration knob.
        UID_MAP_FILE="${MDCLAW_UID_MAP_FILE:-/proc/self/uid_map}"
        if [ -r "$UID_MAP_FILE" ] \
            && ! grep -qE '^[[:space:]]*0[[:space:]]+0[[:space:]]+4294967295' \
                "$UID_MAP_FILE"; then
            echo "Warning: running $CONTAINER_CMD inside a user namespace;" \
                "the SIF will be extracted on every call instead of mounted." \
                "Drop the unshare wrapper and use --no-home with a neutral" \
                "bind path instead." >&2
        fi
        NV_FLAG=""
        if command -v nvidia-smi &>/dev/null; then
            NV_FLAG="--nv"
        fi
        # Bind cwd at the same absolute path used by the host. This keeps
        # relative and absolute arguments in one namespace, matching Docker.
        # PKG_ROOT is bound and put on PYTHONPATH so the container runs the
        # same mdclaw source as the host-side native tools (the image's baked
        # package is only the dependency layer).
        CWD="$(pwd)"
        # Arrays, not word-split strings: a bind path may contain spaces.
        # Expanded as ${arr[@]+"${arr[@]}"}: under `set -u`, bash 3.2 — still
        # the system bash on macOS — treats "${arr[@]}" on an empty array as an
        # unbound variable and aborts, where bash 4.4+ expands it to nothing.
        PKG_BIND=()
        if [ "$PKG_ROOT" != "$CWD" ]; then
            PKG_BIND=(--bind "${PKG_ROOT}:${PKG_ROOT}")
        fi
        NV=()
        [ -n "$NV_FLAG" ] && NV=("$NV_FLAG")
        # Absolute paths in the arguments (--job-dir, --study-dir, a reference
        # PDB inside a JSON argument) are bound too: a job_dir outside the
        # working directory is otherwise invisible in the container and the
        # tools report "no progress.json; bootstrap the job" for a job that
        # exists (2026-09-21).
        ARG_BIND=()
        while IFS= read -r _dir; do
            [ -n "$_dir" ] && ARG_BIND+=(--bind "${_dir}:${_dir}")
        done < <(_mdclaw_arg_bind_dirs "$CWD" "$PKG_ROOT" "$@")
        exec "$CONTAINER_CMD" exec ${NV[@]+"${NV[@]}"} \
            --bind "${CWD}:${CWD}" ${PKG_BIND[@]+"${PKG_BIND[@]}"} ${ARG_BIND[@]+"${ARG_BIND[@]}"} \
            --pwd "${CWD}" \
            --env PYTHONPATH="${PKG_ROOT}${PYTHONPATH:+:$PYTHONPATH}" \
            "$SIF" "$@"
        ;;
    docker)
        GPU_FLAG=""
        if command -v nvidia-smi &>/dev/null; then
            GPU_FLAG="--gpus all"
        fi
        # Map host user so output files have correct ownership (Linux only)
        USER_FLAG=""
        if [ "$(uname -s)" = "Linux" ]; then
            USER_FLAG="--user $(id -u):$(id -g)"
        fi
        # Bind cwd at its absolute path so absolute paths in arguments still resolve.
        # This mirrors Singularity's default behavior of binding $HOME. PKG_ROOT
        # is mounted and put on PYTHONPATH for the same reason as the Singularity
        # branch: the container must run this checkout, not the image's baked
        # package, or host-side native tools and container tools drift apart.
        CWD="$(pwd)"
        # Arrays, not word-split strings: a mount path may contain spaces.
        PKG_MOUNT=()
        if [ "$PKG_ROOT" != "$CWD" ]; then
            PKG_MOUNT=(-v "${PKG_ROOT}:${PKG_ROOT}")
        fi
        GPU=()
        [ -n "$GPU_FLAG" ] && GPU=($GPU_FLAG)
        USER_ARGS=()
        [ -n "$USER_FLAG" ] && USER_ARGS=($USER_FLAG)
        ARG_MOUNT=()
        while IFS= read -r _dir; do
            [ -n "$_dir" ] && ARG_MOUNT+=(-v "${_dir}:${_dir}")
        done < <(_mdclaw_arg_bind_dirs "$CWD" "$PKG_ROOT" "$@")
        exec docker run --rm --pull=never ${GPU[@]+"${GPU[@]}"} ${USER_ARGS[@]+"${USER_ARGS[@]}"} \
            -v "${CWD}:${CWD}" ${PKG_MOUNT[@]+"${PKG_MOUNT[@]}"} ${ARG_MOUNT[@]+"${ARG_MOUNT[@]}"} -w "${CWD}" \
            -e PYTHONPATH="${PKG_ROOT}${PYTHONPATH:+:$PYTHONPATH}" \
            "$DOCKER_IMAGE" "$@"
        ;;
    local)
        exec "$@"
        ;;
esac
}
