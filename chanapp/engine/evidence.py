"""Explain only facts carried by native morphological points."""


def build_evidence(signals: list[dict], structure: dict) -> list[dict]:
    cards = []
    for signal in signals:
        unit = '段' if signal['level'] == 'seg' else '笔'
        state = '已确认' if signal['status'] == 'confirmed' else '形成中'
        text = f"{unit}级形态 {', '.join(signal['types'])}；{state}。力度算法：{signal['macd_algo']}。"
        relation = signal.get('related_bsp1')
        if relation:
            text += f"关联一类点 {relation['dt']} @ {relation['price']:.2f}。"
        text += '默认形态规则不要求 MACD 力度比例过滤。'
        cards.append(dict(type=signal['label'], label=signal['label'], dt=signal['dt'], price=signal['price'],
            side=signal['side'], level=signal['level'], types=signal['types'], status=signal['status'],
            forming=signal['forming'], text=text,
            detail={key: signal.get(key) for key in ('structure_ref', 'related_bsp1', 'features', 'macd_algo', 'last_sure_pos')}))
    return cards
