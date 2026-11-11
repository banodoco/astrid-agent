"""Fixed 80-pair continuation, with prior evidence excluded by construction."""
FULL_REF = 'refs/heads/scratch/r12-a2-resume-080-20261111'
RANGES = {'028-034': tuple(f'{n:03}' for n in range(28,35)),
          '035-041': tuple(f'{n:03}' for n in range(35,42)),
          '041-041': ('041',),
          '042-052': tuple(f'{n:03}' for n in range(42,53))}
SIDES = ('r10', 'r11', 'base', 'candidate')
SIDE_RANGES = {'r10': ('041-041','042-052'), 'r11': ('035-041','042-052'),
               'base': ('028-034','035-041','042-052'), 'candidate': ('028-034','035-041','042-052')}

def group_for(side, group_range, slot):
    if side not in SIDES or group_range not in SIDE_RANGES[side] or type(slot) is not int or not 1 <= slot <= 11:
        raise ValueError('invalid matrix scope')
    groups = RANGES[group_range]
    return groups[slot-1] if slot <= len(groups) else None
