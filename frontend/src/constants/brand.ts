// Modified for Xuheng: product identity; source notices live in About.
export const BRAND = {
  productName: 'Xuheng',
  chineseName: '叙衡',
  displayName: '叙衡',
  tagline: '写下故事，核对每一处依据',
  descriptor: 'AI 创作与一致性审校工作台',
  team: '叙衡项目',
  credit: '创作 · 检索 · 审校',
} as const

export const BRAND_COPY = {
  short: BRAND.displayName,
  compact: `${BRAND.chineseName} · ${BRAND.tagline}`,
  full: `${BRAND.displayName}｜${BRAND.credit}`,
  social: BRAND.tagline,
} as const
