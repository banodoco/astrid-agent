import { describe, expect, it } from 'bun:test';
import { sessionConfigModels } from './sessionConfigOptions.ts';
import { defaultThoughtLevel } from './localAgents.ts';

const model = { id: 'model', name: 'Model', category: 'model', type: 'select', currentValue: 'opus', options: [{ value: 'opus', name: 'Opus' }] };
const recommended = (value: string) => ({ jetbrains: { air: { version: 1, recommendedValue: value } } });

describe('ACP session config options', () => {
  it('reads Codex base model and reasoning effort, ahead of its legacy model ID with effort suffix', () => {
    // codex-acp@2.2.2 dist/index.js createReasoningEffortConfigOption
    const parsed = sessionConfigModels({ models: { currentModelId: 'opus-high', availableModels: [] }, configOptions: [model, {
      id: 'reasoning_effort', name: 'Reasoning effort', description: 'How much reasoning effort the model should use',
      category: 'thought_level', type: 'select', currentValue: 'high',
      options: [{ value: 'low', name: 'Low', description: 'Fast' }, { value: 'high', name: 'High', description: 'Careful' }], _meta: recommended('low'),
    }] });
    expect(parsed).toMatchObject({ currentModelId: 'opus', modelConfigId: 'model', thoughtLevel: { configId: 'reasoning_effort', currentValue: 'high', recommendedValue: 'low' } });
    expect(defaultThoughtLevel(parsed!.thoughtLevel!)).toBe('low');
  });

  it.each([false, true])('reads Claude effort with recommendation capability %s', withRecommendation => {
    // claude-agent-acp@0.93.0 dist/session-effort.js buildEffortConfigOption
    const levels = [{ value: 'low', name: 'Low' }, { value: 'medium', name: 'Medium' }, { value: 'high', name: 'High' }];
    const effort = { id: 'effort', name: 'Effort', description: 'Available effort levels for this model', category: 'thought_level', type: 'select',
      currentValue: withRecommendation ? 'medium' : 'default', options: [...(withRecommendation ? [] : [{ value: 'default', name: 'Default' }]), ...levels],
      ...(withRecommendation ? { _meta: recommended('medium') } : {}) };
    const parsed = sessionConfigModels({ configOptions: [model, effort] });
    expect(parsed?.thoughtLevel?.options).toEqual(effort.options);
    expect(defaultThoughtLevel(parsed!.thoughtLevel!)).toBe(withRecommendation ? 'medium' : 'default');
  });

  it('identifies categories with opaque IDs and handles OMP grouped models and thinking', () => {
    const parsed = sessionConfigModels({ configOptions: [{ ...model, id: 'model-picker', options: [{ group: 'provider', name: 'Provider', options: model.options }] }, {
      id: 'thinking', name: 'Thinking', category: 'thought_level', type: 'select', currentValue: 'auto', options: [{ value: 'off', name: 'Off' }, { value: 'auto', name: 'Auto' }, { value: 'high', name: 'High' }],
    }] });
    expect(parsed).toMatchObject({ modelConfigId: 'model-picker', availableModels: [{ modelId: 'opus', name: 'Opus' }], thoughtLevel: { currentValue: 'auto' } });
  });

  it('names levels in sentence case', () => {
    const parsed = sessionConfigModels({ configOptions: [model, { id: 'effort', name: 'Effort', category: 'thought_level', type: 'select', currentValue: 'high',
      options: [{ value: 'high', name: 'HIGH' }, { value: 'xhigh', name: 'Xhigh' }, { value: 'max', name: 'Max' }] }] });
    expect(parsed?.thoughtLevel?.options.map(option => option.name)).toEqual(['High', 'Extra high', 'Max']);
  });

  it.each(['Hermes', 'Astrid'])('does not invent levels for %s without a thought_level option', () => {
    expect(sessionConfigModels({ models: { currentModelId: 'a', availableModels: [{ modelId: 'a', name: 'A' }] } })?.thoughtLevel).toBeUndefined();
    expect(sessionConfigModels({ configOptions: [model] })?.thoughtLevel).toBeUndefined();
  });
});
