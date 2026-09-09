"""Explain only facts carried by native morphological points."""


def build_evidence(signals: list[dict], structure: dict) -> list[dict]:
    cards = []
    for signal in signals:
        unit = '段' if signal['level'] == 'seg' else '笔'
        state = '已确认' if signal['status'] == 'confirmed' else '形成中'
        strength = signal.get('strength', {})
        metric = strength.get('metric', signal['macd_algo'])
        metric_name = {'peak': 'MACD同向柱峰值', 'slope': '价格变化斜率'}.get(metric, metric)
        text = f"{unit}级形态 {', '.join(signal['types'])}；{state}。力度算法：{metric_name}。"
        context = signal.get('context', {})
        if context.get('origin') == 'zero_center':
            source = '自身为无中枢一类点' if context.get('origin_source') == 'self' else '关联无中枢一类点'
            text += f'扩展来源：{source}。'
        count = context.get('zs_count')
        if count is not None:
            text += f'所在父段有 {count} 个中枢。'
        if strength.get('state') in ('weaker', 'equal', 'stronger'):
            comparison = {'weaker': '减弱', 'equal': '持平', 'stronger': '增强'}[strength['state']]
            text += f"原生力度比 {strength['value']:.4g}（{comparison}）。"
        else:
            text += '此点无可用原生力度比。'
        relation = signal.get('related_bsp1')
        if relation:
            text += f"关联一类点 {relation['dt']} @ {relation['price']:.2f}。"
        text += '力度仅作标注，不作硬过滤。'
        cards.append(dict(type=signal['label'], label=signal['label'], dt=signal['dt'], price=signal['price'],
            side=signal['side'], level=signal['level'], types=signal['types'], status=signal['status'],
            forming=signal['forming'], text=text,
            detail={key: signal.get(key) for key in ('structure_ref', 'related_bsp1', 'features', 'macd_algo', 'last_sure_pos', 'context', 'strength')}))
    return cards
