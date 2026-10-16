export const resolveParams = (clip) => {
    if (clip.clipType === 'text') {
        return clip.text;
    }
    return clip.params;
};
