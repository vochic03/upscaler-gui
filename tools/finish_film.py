#!/usr/bin/env python3
"""Finish the film: the 22 pieces the disk-full killed.

This RENTS A GPU. It refuses to run without --yes, and the estimate is
printed before anything is created.
"""
import os, sys, time, shutil
sys.path.insert(0, os.path.expanduser('~/upscaler-gui'))
import keys, rental, cloud, ffbin

JOB = os.path.expanduser('~/upscaler-gui/work/jobs/6320fce5a630')
PARTS = os.path.join(JOB, 'parts')
UPLOAD = os.path.join(JOB, 'upload.mp4')
LOCAL = '/tmp/filmparts'
IMAGE = 'bzz1/upscaler:seedvr2'
GPU = 'NVIDIA L40S'
RESOLUTION = 1080
SCALE = 4

def log(m): print(m, flush=True)

def main():
    yes = '--yes' in sys.argv
    frames, fps, w, h = cloud.probe_video(UPLOAD)
    total = len(range(0, frames, cloud.CHUNK_FRAMES))
    have = set()
    for n in os.listdir(PARTS):
        if n.startswith('answer-') and n.endswith('.mp4'):
            have.add(int(n[7:-4]))
    missing = [s for s in range(0, frames, cloud.CHUNK_FRAMES) if s not in have]
    pieces = len(missing)
    per = 64.0
    card_seconds = pieces * per
    rate = 1.09
    spin = 240.0
    cost = (card_seconds + spin) / 3600.0 * rate
    log(f'the guide: {frames} frames of {w}x{h} at {fps:.3f} fps')
    log(f'{total} pieces in the film, {len(have)} already done, {pieces} to go')
    log(f'card time: {card_seconds/60:.0f} min at {rate:.2f}/h + {spin/60:.0f} min spin-up')
    log(f'ESTIMATE: about ${cost:.2f} (worst case ${cost*1.6:.2f} if the pool is empty and it waits)')
    if not yes:
        log('')
        log('This would rent a real GPU. Run it with --yes to go ahead.')
        return 1
    os.makedirs(LOCAL, exist_ok=True)
    key = keys.resolve('runpod')
    r = rental.RunPod(key, gpu=GPU, image=IMAGE, engine='seedvr2',
                      name='upscaler-resume', disk=60, port=8000, log=log)
    r.approve({'one_clip_cost': round(cost, 2), 'price_per_hour': r.price_per_hour()})
    answers = []
    try:
        r.create()
        endpoint = r.wait_ready(timeout=900)
        engines = (cloud.health(endpoint) or {}).get('engines') or []
        if engines and 'seedvr2' not in engines:
            raise RuntimeError('the card cannot run seedvr2: ' + ', '.join(engines))
        log(f'the worker is ready: {engines}')
        for i, start in enumerate(range(0, frames, cloud.CHUNK_FRAMES)):
            end = min(start + cloud.CHUNK_FRAMES, frames)
            ans = os.path.join(PARTS, f'answer-{start:07d}.mp4')
            if start in have:
                answers.append(ans)
                continue
            part = cloud.split_video(UPLOAD, LOCAL, start, end, fps)
            t0 = time.time()
            cloud.upscale(endpoint, part, ans, scale=SCALE, model=None,
                          engine='seedvr2', resolution=RESOLUTION,
                          log=lambda m: log('   ' + str(m)))
            log(f'piece {i+1}/{total}: frames {start}-{end} in {time.time()-t0:.0f}s')
            answers.append(ans)
        out = '/tmp/film_full_ai.mp4'   # local disk: the iCloud folder is slow for a big write
        log('joining all the pieces ...')
        cloud.concat_parts(answers, out, log=log)
        got, _, _, _ = cloud.probe_video(out)
        log(f'joined: {got} frames (the film has {frames})')
        if got != frames:
            raise RuntimeError(f'the joined video has {got} frames, not {frames}')
    finally:
        log('returning the GPU ...')
        r.destroy()
    log('done')
    return 0

if __name__ == '__main__':
    raise SystemExit(main())
