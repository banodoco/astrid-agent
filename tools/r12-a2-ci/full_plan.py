"""Frozen continuation matrix and single-creation event."""
FULL_REF = 'refs/heads/scratch/r12-a2-resume-020-052-20261111'
RANGES = {'020-030': tuple(f'{n:03}' for n in range(20,31)),
          '031-041': tuple(f'{n:03}' for n in range(31,42)),
          '042-052': tuple(f'{n:03}' for n in range(42,53))}
SIDES = ('r10', 'r11', 'base', 'candidate')

def group_for(side, group_range, slot):
    if side not in SIDES or group_range not in RANGES or type(slot) is not int or not 1 <= slot <= 12:
        raise ValueError('invalid matrix scope')
    groups = RANGES[group_range]
    if side == 'r10' and group_range == '020-030': groups = groups[1:]
    return groups[slot-1] if slot <= len(groups) else None
