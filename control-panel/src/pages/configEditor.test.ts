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
  it('uses exact and wildcard schema paths', () => {
    const candidates = schemaPathCandidates([
      'providers',
      'internal_gateway',
      'model_grouper',
      0,
      'pricing',
      'agnes-2.0-flash',
      'prompt',
    ])

    expect(candidates).toEqual([
      'providers.internal_gateway.model_grouper[].pricing.agnes-2.0-flash.prompt',
      'providers.*.model_grouper[].pricing.*.prompt',
    ])
  })

  it('uses wildcard schema paths for generation model capability scores', () => {
    expect(schemaPathCandidates([
      'generation_optimization',
      'model_router',
      'model_capabilities',
      'custom-image-model',
    ])).toEqual([
      'generation_optimization.model_router.model_capabilities.custom-image-model',
      'generation_optimization.model_router.model_capabilities.*',
    ])
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

  it('matches custom provider descriptions and leaf pricing descriptions', () => {
    const items: ConfigSchemaItem[] = [
      {
        path: 'providers.*.model_grouper[].models[].features',
        module: 'providers',
        description: '运行时能力',
        value_type: 'string[]',
        editor: 'token_list',
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
    expect(features?.schemaEditor).toBe('token_list')
    expect(prompt?.description).toBe('输入 token 单价')
  })

  it('parses token-list inputs and keeps other arrays on JSON', () => {
    expect(parseCompactStringArray('tool_calling, structured_output')).toEqual([
      'tool_calling',
      'structured_output',
    ])
    expect(() => parseCompactStringArray('tool_calling,')).toThrow('列表项不能为空')
    expect(parseEditedValue('a, b', [], 'string[]', 'token_list')).toEqual(['a', 'b'])
    expect(() => parseEditedValue('1, nope', [1, 2], 'integer[]')).toThrow('JSON 格式无效')
    expect(() => parseEditedValue('true, typo', [true], 'boolean[]')).toThrow('JSON 格式无效')
  })

  it('uses compact inputs for all simple string arrays and JSON when items contain commas', () => {
    const value = ['alpha,beta', 'gamma']
    expect(isCompactStringArray(['alpha', 'beta'], 'string[]')).toBe(true)
    expect(valueToText(['agnes-2.0-flash'], 'string[]')).toBe('agnes-2.0-flash')
    expect(isCompactStringArray(value, 'string[]', 'token_list')).toBe(false)
    expect(valueToText(value, 'string[]', 'token_list')).toBe('[\n  "alpha,beta",\n  "gamma"\n]')
  })
})
