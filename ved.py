#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""FFmpeg utilities wrapper."""
import argparse
import os
import re
import shutil
import subprocess
import sys


FFMPEG = 'ffmpeg' if shutil.which('ffmpeg') else './ffmpeg.exe'
SUBCOMMANDS = frozenset({'concat', 'merge-fade', 'side-by-side'})


def run(cmd):
    cmd[0] = FFMPEG
    print(' '.join(str(c) for c in cmd))
    subprocess.run(cmd, check=True)


def get_duration(path):
    result = subprocess.run([FFMPEG, '-i', path], capture_output=True, text=True)
    match = re.search(r'Duration: (\d+):(\d+):(\d+(?:\.\d+)?)', result.stderr)
    h, m, s = match.groups()
    return int(h) * 3600 + int(m) * 60 + float(s)


def scale_and_pad(res):
    w, h = res.split('x')
    return f'scale={w}:{h}:force_original_aspect_ratio=decrease,pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,setsar=1'


def parse_range(s):
    if s.startswith('~'):
        return None, s[1:]
    elif s.endswith('~'):
        return s[:-1], None
    else:
        start, _, end = s.partition('~')
        return start, end


# --- process mode ---

def cmd_process(args):
    extra_inputs = []
    pip_idx = None
    sound_idx = None

    if args.pip:
        pip_idx = 1 + len(extra_inputs)
        extra_inputs.append(args.pip)

    sound_file = args.replace_sound or args.add_sound
    if sound_file:
        sound_idx = 1 + len(extra_inputs)
        extra_inputs.append(sound_file)

    cmd = ['ffmpeg']

    # mute needs lavfi as the first input before main video
    if args.mute:
        cmd += ['-f', 'lavfi', '-i', 'anullsrc=channel_layout=stereo:sample_rate=44100']
        main_idx = 1
    else:
        main_idx = 0
        if args.trim:
            start, end = parse_range(args.trim)
            if start: cmd += ['-ss', start]
            if end: cmd += ['-to', end]

    cmd += ['-i', args.input]
    for inp in extra_inputs:
        cmd += ['-i', inp]

    # Video filter chain
    vfilters = []
    if args.crop:
        res, pos = args.crop.split(',', 1)
        w, h = res.split('x')
        x, y = pos.split(',')
        vfilters.append(f'crop={w}:{h}:{x}:{y}')
        if args.scale:
            vfilters.append(scale_and_pad(args.scale))
    if args.expand:
        ew, eh = args.expand.split('x')
        vfilters.append(f'scale={ew}:{eh}:force_original_aspect_ratio=decrease,pad={ew}:{eh}:-1:-1:color=black')
    if args.fps is not None:
        vfilters.append(f'fps={args.fps}')
    if args.speed is not None:
        vfilters.append(f'setpts={1/args.speed}*PTS')

    needs_fc = pip_idx is not None or args.add_sound

    if needs_fc:
        fc = []
        v_map = f'{main_idx}:v'

        if vfilters or pip_idx is not None:
            v_src = f'[{main_idx}:v]'
            if vfilters:
                fc.append(f'[{main_idx}:v]{",".join(vfilters)}[vf]')
                v_src = '[vf]'
            if pip_idx is not None:
                x, y = args.pip_position.split(',')
                fc.append(f'[{pip_idx}]scale=iw/{args.pip_size}:ih/{args.pip_size}[pip]')
                fc.append(f'{v_src}[pip]overlay={x}:{y}[vout]')
                v_map = '[vout]'
            else:
                fc[-1] = fc[-1].replace('[vf]', '[vout]')
                v_map = '[vout]'

        if args.add_sound:
            fc.append(f'[{main_idx}:a][{sound_idx}:a]amix=inputs=2:duration=longest[aout]')

        if fc:
            cmd += ['-filter_complex', ';'.join(fc)]

        cmd += ['-map', v_map]

        if args.add_sound:
            cmd += ['-map', '[aout]']
        elif args.mute:
            cmd += ['-an']
        elif args.replace_sound:
            cmd += ['-map', f'{sound_idx}:a', '-y', '-shortest']
        else:
            cmd += ['-map', f'{main_idx}:a', '-c:a', 'copy']

    else:
        if vfilters:
            cmd += ['-filter:v', ','.join(vfilters)]

        if args.mute:
            cmd += ['-c:v', 'copy', '-c:a', 'aac', '-shortest']
        elif args.replace_sound:
            cmd += ['-map', f'{main_idx}:v', '-map', f'{sound_idx}:a', '-c:v', 'copy', '-y', '-shortest']
        else:
            cmd += ['-c:a', 'copy']

    cmd.append(args.output)
    run(cmd)


# --- subcommands ---

def cmd_concat(args):
    *videos, output = args.files
    list_path = '_concat_list.txt'
    with open(list_path, 'w') as f:
        for v in videos:
            f.write(f"file '{v}'\n")
    try:
        run(['ffmpeg', '-safe', '0', '-f', 'concat', '-i', list_path, '-c', 'copy', output])
    finally:
        os.remove(list_path)


def build_merge_fade_filter(durations, fade):
    n = len(durations)
    parts = []
    for i in range(n):
        parts.append(f'[{i}]settb=AVTB[{i}:v]')
    for i in range(n):
        parts.append(f'[{i}]atrim=0:{durations[i]}[{i}:a]')
    cumulative = 0.0
    for i in range(n - 1):
        cumulative += durations[i]
        in_left = '[0:v]' if i == 0 else f'[v{i-1}]'
        out = ',format=yuv420p[video]' if i == n - 2 else f'[v{i}]'
        parts.append(f'{in_left}[{i+1}:v]xfade=transition=fade:duration={fade}:offset={cumulative - (i+1)*fade}{out}')
    for i in range(n - 1):
        in_left = '[0:a]' if i == 0 else f'[a{i-1}]'
        out = '[audio]' if i == n - 2 else f'[a{i}]'
        parts.append(f'{in_left}[{i+1}:a]acrossfade=d={fade}:c1=tri:c2=tri{out}')
    return ';'.join(parts)


def cmd_merge_fade(args):
    *videos, output = args.files
    fade = args.fade
    print('Getting video durations...')
    durations = []
    for v in videos:
        d = get_duration(v)
        print(f'  {v}: {d:.2f}s')
        durations.append(d)
    total = sum(durations)
    print(f'\nEstimated output duration: {total:.2f}s ({total / 60:.2f} min)')
    if args.dry_run:
        return
    cmd = (['ffmpeg', '-vsync', '0']
           + [x for v in videos for x in ('-i', v)]
           + ['-filter_complex', build_merge_fade_filter(durations, fade),
              '-b:v', '10M', '-map', '[audio]', '-map', '[video]', output])
    run(cmd)


def cmd_side_by_side(args):
    run(['ffmpeg', '-i', args.left, '-i', args.right, '-filter_complex', 'hstack', args.output])


# --- argument parsers ---

def print_help():
    print('Usage: ved.py INPUT [options] OUTPUT')
    print()
    print('  --trim START~END         Trim range: START~END, START~, or ~END')
    print('  --crop WxH,X,Y           Crop dimensions (e.g. 960x1080,0,0)')
    print('  --scale [WxH]            With --crop: scale result (default: 1920x1080)')
    print('  --expand [WxH]           Fit to target resolution with black bars (default: 1920x1080)')
    print('  --fps FPS                Change framerate')
    print('  --speed FACTOR           Speed (2=2x faster, 0.5=half speed)')
    print('  --mute                   Replace audio with silent track')
    print('  --pip OVERLAY            Picture-in-picture overlay file')
    print('  --pip-size N             PIP size divisor (default: 3 = 1/3)')
    print('  --pip-position X,Y       PIP position (default: 10,10 = top-left)')
    print('  --replace-sound FILE     Replace audio with sound file')
    print('  --add-sound FILE         Mix sound file with existing audio')
    print()
    print('Subcommands:')
    print('  ved.py concat INPUT [INPUT ...]')
    print('  ved.py merge-fade FADE INPUT [INPUT ...] [--dry-run]')
    print('  ved.py side-by-side LEFT RIGHT OUTPUT')


def parse_process(argv):
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument('input', metavar='INPUT')
    parser.add_argument('--trim', metavar='START~END')
    parser.add_argument('--crop', metavar='WxH,X,Y')
    parser.add_argument('--scale', nargs='?', const='1920x1080', metavar='WxH')
    parser.add_argument('--expand', nargs='?', const='1920x1080', metavar='WxH')
    parser.add_argument('--fps', type=int, metavar='FPS')
    parser.add_argument('--speed', type=float, metavar='FACTOR')
    parser.add_argument('--mute', action='store_true')
    parser.add_argument('--pip', metavar='OVERLAY')
    parser.add_argument('--pip-size', type=int, default=3, metavar='N')
    parser.add_argument('--pip-position', default='10,10', metavar='X,Y')
    parser.add_argument('--replace-sound', metavar='FILE')
    parser.add_argument('--add-sound', metavar='FILE')
    parser.add_argument('output', metavar='OUTPUT')
    return parser.parse_args(argv)


def parse_subcommand(argv):
    parser = argparse.ArgumentParser(add_help=False)
    sub = parser.add_subparsers(dest='command')

    p = sub.add_parser('concat')
    p.add_argument('files', nargs='+', metavar='INPUT')

    p = sub.add_parser('merge-fade')
    p.add_argument('fade', type=float, metavar='FADE')
    p.add_argument('files', nargs='+', metavar='INPUT')
    p.add_argument('--dry-run', action='store_true')

    p = sub.add_parser('side-by-side')
    p.add_argument('left', metavar='LEFT')
    p.add_argument('right', metavar='RIGHT')
    p.add_argument('output', metavar='OUTPUT')

    return parser.parse_args(argv)


def main():
    if len(sys.argv) < 2 or sys.argv[1] in ('help', '-h', '--help'):
        print_help()
        return

    if sys.argv[1] in SUBCOMMANDS:
        args = parse_subcommand(sys.argv[1:])
        {'concat': cmd_concat, 'merge-fade': cmd_merge_fade, 'side-by-side': cmd_side_by_side}[args.command](args)
    else:
        args = parse_process(sys.argv[1:])
        cmd_process(args)


if __name__ == '__main__':
    main()
