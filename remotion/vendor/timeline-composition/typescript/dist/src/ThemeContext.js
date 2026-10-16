import { jsx as _jsx } from "react/jsx-runtime";
// Sprint 5: physically moved here from tools/remotion/src/ThemeContext.tsx.
// The theme-api re-export now points at this in-package module.
import { createContext, useContext } from 'react';
export const DEFAULT_THEME = {
    id: 'banodoco-default',
    visual: {
        color: {
            fg: '#ffffff',
            bg: '#000000',
            accent: '#ffffff',
        },
        type: {
            families: {
                heading: 'Georgia, serif',
                body: 'Georgia, serif',
            },
            size: {
                base: 64,
                small: 36,
                large: 96,
            },
            weight: {
                normal: 400,
                bold: 700,
            },
            lineHeight: 1.1,
        },
        motion: {
            fadeMs: 250,
        },
        canvas: {
            width: 1920,
            height: 1080,
            fps: 30,
        },
    },
};
export const toRuntimeTheme = (theme) => {
    return {
        ...theme,
        color: theme.visual.color,
        type: theme.visual.type,
        motion: theme.visual.motion,
    };
};
const DEFAULT_RUNTIME_THEME = toRuntimeTheme(DEFAULT_THEME);
const ThemeContext = createContext(DEFAULT_RUNTIME_THEME);
export const ThemeProvider = ({ children, value, }) => {
    return _jsx(ThemeContext.Provider, { value: value === undefined ? DEFAULT_RUNTIME_THEME : toRuntimeTheme(value), children: children });
};
export const useTheme = () => {
    return useContext(ThemeContext);
};
