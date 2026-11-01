import {isValidElement} from 'react';
import type {ReactElement} from 'react';

import type {AnimationComponentProps} from '../../../../shared/element_contracts';

// Builtin pass-through: themes override this with real animation behavior.
export default function TypeOn(props: AnimationComponentProps): ReactElement | null {
  return isValidElement(props.children) ? props.children : null;
}
