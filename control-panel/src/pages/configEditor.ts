export type ConfigValue = string | number | boolean | null | ConfigValue[] | { [key: string]: ConfigValue }
export type ConfigObject = Record<string, ConfigValue>
export type ConfigPathSegment = string | number

export interface ConfigSchemaItem {
  path: string
  module: string
  description: string
  value_type?: string
  editor?: string
}

export interface ConfigRow {
  path: string
  group: string
  segments: ConfigPathSegment[]
  value: ConfigValue
  description: string
  schemaType?: string
  schemaEditor?: string
}

export function isPlainObject(value: unknown): value is Record<string, ConfigValue> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

export function toConfigValue(value: unknown): ConfigValue {
  if (value === null || typeof value === 'string' || typeof value === 'number' || typeof value === 'boolean') return value
  if (Array.isArray(value)) return value.map(toConfigValue)
  if (isPlainObject(value)) {
    const out: Record<string, ConfigValue> = {}
    for (const [key, child] of Object.entries(value)) out[key] = toConfigValue(child)
    return out
  }
  return String(value)
}

function compactStringValue(value: string): boolean {
  return value === value.trim() && !value.includes(',') && !value.includes('\n') && !value.includes('\r')
}

export function isCompactStringArray(
  value: ConfigValue,
  schemaType?: string,
  _schemaEditor?: string,
): value is string[] {
  if (schemaType !== 'string[]' || !Array.isArray(value)) return false
  return value.every(item => typeof item === 'string' && compactStringValue(item))
}

export function parseCompactStringArray(input: string): string[] {
  if (!input.trim()) return []
  const parts = input.split(',').map(part => part.trim())
  if (parts.some(part => part.length === 0)) throw new Error('列表项不能为空')
  return parts
}

export function valueToText(
  value: ConfigValue,
  schemaType?: string,
  schemaEditor?: string,
): string {
  if (typeof value === 'string') return value
  if (typeof value === 'number' || typeof value === 'boolean') return String(value)
  if (value === null) return 'null'
  if (isCompactStringArray(value, schemaType, schemaEditor)) return value.join(', ')
  return JSON.stringify(value, null, 2)
}

export function parseEditedValue(
  input: string,
  previous: ConfigValue,
  schemaType?: string,
  schemaEditor?: string,
): ConfigValue {
  if (typeof previous === 'boolean') return input === 'true'
  if (typeof previous === 'number') {
    if (input.trim() === '') throw new Error('数字不能为空')
    const parsed = Number(input)
    if (!Number.isFinite(parsed)) throw new Error('数字格式无效')
    return parsed
  }
  if (previous === null) {
    if (input.trim() === '' || input.trim() === 'null') return null
    try { return toConfigValue(JSON.parse(input)) } catch { return input }
  }
  if (isCompactStringArray(previous, schemaType, schemaEditor)) return parseCompactStringArray(input)
  if (Array.isArray(previous) || isPlainObject(previous)) {
    try { return toConfigValue(JSON.parse(input)) } catch { throw new Error('JSON 格式无效') }
  }
  return input
}

function simplePathKey(value: string): boolean {
  return /^[A-Za-z_][A-Za-z0-9_-]*$/.test(value)
}

export function displayPath(segments: ConfigPathSegment[]): string {
  let output = ''
  for (const segment of segments) {
    if (typeof segment === 'number') {
      output += `[${segment}]`
    } else if (!output) {
      output = segment
    } else if (simplePathKey(segment)) {
      output += `.${segment}`
    } else {
      output += `[${JSON.stringify(segment)}]`
    }
  }
  return output
}

function schemaTokens(segments: ConfigPathSegment[], wildcard: boolean): string[] {
  const tokens: string[] = []
  const previousStrings: string[] = []
  for (const segment of segments) {
    if (typeof segment === 'number') {
      if (tokens.length > 0) tokens[tokens.length - 1] += '[]'
      continue
    }
    let value = segment
    if (wildcard) {
      if (previousStrings.length === 1 && previousStrings[0] === 'providers') {
        value = '*'
      } else if (
        previousStrings.length === 2
        && previousStrings[0] === 'plugin_runtime'
        && previousStrings[1] === 'plugins'
      ) {
        value = '*'
      } else if (previousStrings.at(-1) === 'pricing' && previousStrings[0] === 'providers') {
        value = '*'
      } else if (
        previousStrings.length === 3
        && previousStrings[0] === 'generation_optimization'
        && previousStrings[1] === 'model_router'
        && previousStrings[2] === 'model_capabilities'
      ) {
        value = '*'
      }
    }
    tokens.push(value)
    previousStrings.push(segment)
  }
  return tokens
}

export function schemaPathCandidates(segments: ConfigPathSegment[]): string[] {
  const exact = schemaTokens(segments, false).join('.')
  const wildcard = schemaTokens(segments, true).join('.')
  return wildcard && wildcard !== exact ? [exact, wildcard] : [exact]
}

function schemaForPath(
  segments: ConfigPathSegment[],
  schema: Map<string, ConfigSchemaItem>,
): ConfigSchemaItem | undefined {
  for (const candidate of schemaPathCandidates(segments)) {
    const item = schema.get(candidate)
    if (item) return item
  }
  return undefined
}

export function flattenConfig(
  value: ConfigValue,
  schema: Map<string, ConfigSchemaItem>,
  segments: ConfigPathSegment[] = [],
): ConfigRow[] {
  const path = displayPath(segments)
  const first = segments.find(segment => typeof segment === 'string')
  const group = typeof first === 'string' ? first : 'root'
  const schemaItem = schemaForPath(segments, schema)
  const description = schemaItem?.description
    ?? (Array.isArray(value)
      ? '配置模板未提供说明；该值为列表，可用 JSON 数组格式编辑。'
      : isPlainObject(value)
        ? '配置模板未提供说明；该值为对象，可用 JSON 对象格式编辑。'
        : '配置模板未提供说明。请在 config.yaml.template 中为该参数补充行内注释。')

  if (Array.isArray(value)) {
    if (value.length === 0 || value.every(item => !isPlainObject(item) && !Array.isArray(item))) {
      return [{
        path,
        group,
        segments,
        value,
        description,
        schemaType: schemaItem?.value_type,
        schemaEditor: schemaItem?.editor,
      }]
    }
    return value.flatMap((item, index) => flattenConfig(item, schema, [...segments, index]))
  }
  if (isPlainObject(value)) {
    const entries = Object.entries(value)
    if (entries.length === 0) {
      return [{
        path,
        group,
        segments,
        value,
        description,
        schemaType: schemaItem?.value_type,
        schemaEditor: schemaItem?.editor,
      }]
    }
    return entries.flatMap(([key, child]) => flattenConfig(child, schema, [...segments, key]))
  }
  return [{
    path,
    group,
    segments,
    value,
    description,
    schemaType: schemaItem?.value_type,
    schemaEditor: schemaItem?.editor,
  }]
}

export function readByPath(root: ConfigValue, segments: ConfigPathSegment[]): ConfigValue {
  let cursor = root
  for (const segment of segments) {
    if (typeof segment === 'number') {
      if (!Array.isArray(cursor) || segment < 0 || segment >= cursor.length) {
        throw new Error(`路径无效: ${displayPath(segments)}`)
      }
      cursor = cursor[segment]
    } else {
      if (!isPlainObject(cursor) || !(segment in cursor)) {
        throw new Error(`路径无效: ${displayPath(segments)}`)
      }
      cursor = cursor[segment]
    }
  }
  return cursor
}

export function writeByPath(root: ConfigValue, segments: ConfigPathSegment[], value: ConfigValue): ConfigValue {
  const cloned = structuredClone(root)
  let cursor = cloned
  for (let index = 0; index < segments.length - 1; index += 1) {
    const segment = segments[index]
    if (typeof segment === 'number') {
      if (!Array.isArray(cursor) || segment < 0 || segment >= cursor.length) {
        throw new Error(`路径无效: ${displayPath(segments)}`)
      }
      cursor = cursor[segment]
    } else {
      if (!isPlainObject(cursor) || !(segment in cursor)) {
        throw new Error(`路径无效: ${displayPath(segments)}`)
      }
      cursor = cursor[segment]
    }
  }
  const last = segments.at(-1)
  if (typeof last === 'number') {
    if (!Array.isArray(cursor) || last < 0 || last >= cursor.length) {
      throw new Error(`路径无效: ${displayPath(segments)}`)
    }
    cursor[last] = value
  } else if (last !== undefined) {
    if (!isPlainObject(cursor)) throw new Error(`路径无效: ${displayPath(segments)}`)
    cursor[last] = value
  }
  return cloned
}

export function groupRows(rows: ConfigRow[]): Array<[string, ConfigRow[]]> {
  const groups = new Map<string, ConfigRow[]>()
  for (const row of rows) {
    const list = groups.get(row.group) ?? []
    list.push(row)
    groups.set(row.group, list)
  }
  return Array.from(groups.entries())
}
