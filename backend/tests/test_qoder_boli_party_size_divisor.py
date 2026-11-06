# -*- coding: utf-8 -*-
u"""玻璃工首格回归臂：party_size 未声明时不再编值当除数。

要求：撤掉改动即红（还原 temporal.py 旧版后 party_size 会重新被编成 1）。
"""
import pytest

def test_party_size_not_becomes_1_when_unstated(temporal_budget_reference):
    """R1: party_size=None 时不编成 1。"""
    result = temporal_budget_reference(
        budget_amount_minor=50000,  # 500 CNY
        day_count=2,
        party_size=None,  # 未声明人数
    )
    
    # 关键断言：未声明时 party_size 应为 None 而非编造的 1
    assert result.party_size is None, "party_size 未声明时应为 None，不该编成 1"
    assert result.per_person_per_day_minor is None, "party_size 为 None 时每人金额应为 None"
    
def test_party_size_declared_used_as_divisor(temporal_budget_reference):
    """R2: party_size 已声明时正常使用。"""
    result = temporal_budget_reference(
        budget_amount_minor=50000,
        day_count=2,
        party_size=3,
    )
    
    # 已声明时正常计算：50000/2/3 = 8333.33 → 8333
    assert result.party_size == 3
    assert result.per_person_per_day_minor == 8333


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
