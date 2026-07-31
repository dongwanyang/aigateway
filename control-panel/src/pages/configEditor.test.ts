import { describe, expect, it } from 'vitest'

import {
  type ConfigSchemaItem,
  displayPath,
  flattenConfig,
  isCompactStringArray,
  parseCompactStringArray,
  parseEditedValue,
  readByPath,
  schemaPathCandidates,
  valueToText,
  writeByPath,
} from './configEditor'

describe('config editor helpers', () => {
  it('uses wildcard schema paths for custom providers and pricing model keys', () => {
    const candidates = schemaPathCandidates([
      'providers',
      'internal_gateway',
      'model_grouper',
      0,
      'pricing',
      'agnes-2.0-flash',
      'prompt',
    ])

    expect(candidates[0]).toBe(
      'providers.internal_gateway.model_grouper[].pricing.agnes-2.0-flash.prompt',
    )
    expect(candidates).toContain(
      'providers.*.model_grouper[].pricing.*.prompt',
    )
    expect(
      candidates.indexOf('providers.*.model_grouper[].pricing.*.prompt'),
    ).toBeLessThan(
      candidates.indexOf('providers.*.model_grouper[].pricing'),
    )
  })

  it('preserves dotted mapping keys during display, read and write', () => {
    const segments = [
      'providers',
      'agnes',
      'model_grouper',
      0,
      'pricing',
      'agnes-2.0-flash',
      'prompt',
    ] as const
    const config = {
      providers: {
        agnes: {
          model_grouper: [{
            pricing: {
              'agnes-2.0-flash': { prompt: 0.02, completion: 1 },
            },
          }],
        },
      },
    }

    expect(displayPath([...segments])).toBe(
      'providers.agnes.model_grouper[0].pricing["agnes-2.0-flash"].prompt',
    )
    expect(readByPath(config, [...segments])).toBe(0.02)
    const updated = writeByPath(config, [...segments], 0.03)
    expect(readByPath(updated, [...segments])).toBe(0.03)
    expect(readByPath(config, [...segments])).toBe(0.02)
  })

  it('matches custom provider descriptions and pricing leaf descriptions', () => {
    const items: ConfigSchemaItem[] = [
      {
        path: 'providers.*.model_grouper[].models[].features',
        module: 'providers',
        description: '运行时能力',
        value_type: 'string[]',
      },
      {
        path: 'providers.*.model_grouper[].pricing.*.prompt',
        module: 'providers',
        description: '输入 token 单价',
        value_type: 'number',
      },
    ]
    const schema = new Map(items.map(item => [item.path, item]))
    const rows = flattenConfig({
      providers: {
        custom_provider: {
          model_grouper: [{
            models: [{ features: ['tool_calling'] }],
            pricing: { 'model.v1': { prompt: 0.1 } },
          }],
        },
      },
    }, schema)

    const features = rows.find(row => row.path.endsWith('.features'))
    const prompt = rows.find(row => row.path.endsWith('].prompt'))
    expect(features?.description).toBe('运行时能力')
    expect(features?.schemaType).toBe('string[]')
    expect(prompt?.description).toBe('输入 token 单价')
  })

  it('parses compact string arrays without coercing numeric or boolean arrays', () => {
    expect(parseCompactStringArray('tool_calling, structured_output')).toEqual([
      'tool_calling',
      'structured_output',
    ])
    expect(() => parseCompactStringArray('tool_calling,')).toThrow('列表项不能为空')
    expect(parseEditedValue('a, b', [], 'string[]')).toEqual(['a', 'b'])
    expect(() => parseEditedValue('1, nope', [1, 2], 'integer[]')).toThrow('JSON 格式无效')
    expect(() => parseEditedValue('true, flase', [true], 'boolean[]')).toThrow('JSON 格式无效')
  })

  it('falls back to JSON for strings that cannot round-trip through commas', () => {
    const value = ['alpha,beta', 'gamma']
    expect(isCompactStringArray(value, 'string[]')).toBe(false)
    expect(valueToText(value, 'string[]')).toBe('[\n  "alpha,beta",\n  "gamma"\n]')
  })
})
