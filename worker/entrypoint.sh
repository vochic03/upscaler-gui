#!/bin/sh
# The entrypoint of the worker.
#
# A rented card's resolver answers with IPv6 addresses for hosts it has no
# IPv6 route for, so every HTTPS call fails - and the weights this worker
# may have to fetch come over HTTPS. Two lines fix it.
#
# The DNS is not the only thing a pod gets wrong: the proxy that carries
# the traffic also caps a request at about a hundred seconds. That is why
# the diffusion work runs as a background job with its own polling route
# rather than inside one request.
printf 'nameserver 8.8.8.8\nnameserver 1.1.1.1\n' > /etc/resolv.conf

# ---------------------------------------------------------------------------
# Where the weights live.
#
# /workspace is the volume disk. It is the part of the pod that survives a
# stop - the container disk is wiped - so it is where the weights belong
# when they are fetched on the card.
#
# It matters more than it looks. DLoRAL's weights are eleven gigabytes and
# Hugging Face serves them at five to nine megabytes a second: twenty
# minutes of a card's time, on every single run. A card that is kept warm
# between jobs keeps the weights on its volume disk, and the next run of
# the same engine starts working straight away.
#
# The directories are symlinked rather than moved, so nothing else has to
# know where they are. If the image has the weights baked in, they are
# left where they are: they came with the pull, which is fast, and the
# image brings them back on the next one.
# ---------------------------------------------------------------------------
if [ -d /workspace ] && [ -w /workspace ]; then
    mkdir -p /workspace/models/dloral /workspace/models/dloral-sd \
             /workspace/models/seedvr2 /workspace/models/star 2>/dev/null || true

    # link <path in the image> <path on the volume disk>
    link() {
        [ -L "$1" ] && return 0
        if [ -e "$1" ]; then
            # An EMPTY folder is not baked-in weights. The STAR repository
            # ships /app/STAR/pretrained_weight, so the link could not be
            # made over it and the weights were fetched onto the container
            # disk - which is wiped when the pod ends - while the volume
            # sat there empty. An empty folder is removed and the link is
            # made instead.
            if [ -d "$1" ] && [ -z "$(ls -A "$1" 2>/dev/null)" ]; then
                rmdir "$1" 2>/dev/null || return 0
            else
                # the image has them baked in - leave them alone
                return 0
            fi
        fi
        # The parent may be missing as well: the DLoRAL weights live two
        # levels down (/app/DLoRAL/preset/models) and the repository ships
        # without that folder, so the link failed with "No such file or
        # directory" - quietly, because of the 2>/dev/null. The weights were
        # then fetched onto the container disk, which is wiped when the pod
        # is terminated, and the volume stayed empty. Make the parent first.
        mkdir -p "$(dirname "$1")" 2>/dev/null || true
        ln -s "$2" "$1" 2>/dev/null || true
    }

    link /app/DLoRAL/preset/models      /workspace/models/dloral
    link /app/DLoRAL/preset_models      /workspace/models/dloral-sd
    link /app/seedvr2_models            /workspace/models/seedvr2
    link /app/STAR/pretrained_weight    /workspace/models/star

    # Say which of them actually ended up on the volume. A link that could
    # not be made is not a small problem here: the weights would be fetched
    # again on every single run.
    for p in /app/DLoRAL/preset/models /app/DLoRAL/preset_models \
             /app/seedvr2_models /app/STAR/pretrained_weight; do
        if [ -L "$p" ]; then
            echo "the weights live on the volume disk: $p -> $(readlink "$p")"
        else
            echo "note: $p is not on the volume disk - the weights there will be" \
                 "fetched onto the container disk and lost when the pod ends"
        fi
    done
fi

exec python3 /app/server.py
