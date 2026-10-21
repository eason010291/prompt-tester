import type { ScriptSet } from './regex'

/** 收集預設與角色卡上的正則腳本（順序：預設 → 角色，ST 的全域正則本工具沒有）。 */
export function collectScriptSets(
  presetScripts: unknown,
  cardScripts: unknown,
  enabled: { preset: boolean; card: boolean },
): ScriptSet[] {
  const sets: ScriptSet[] = []
  if (enabled.preset && Array.isArray(presetScripts)) sets.push({ source: '預設', scripts: presetScripts })
  if (enabled.card && Array.isArray(cardScripts)) sets.push({ source: '角色', scripts: cardScripts })
  return sets
}
