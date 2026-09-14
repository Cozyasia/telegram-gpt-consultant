# -*- coding: utf-8 -*-
"""Small offline guards for Phuket mirror text handling."""

import phuket_mirror


def test_strip_source_promo_removes_source_contacts():
    text = (
        "Проект на Пхукете\nЦена 6 900 000 THB\n\n"
        "Планируете приобретение недвижимости\n"
        "@vladvesi\nhttps://t.me/thailandsell"
    )
    out = phuket_mirror._strip_source_promo(text)
    assert "6 900 000" in out
    assert "@vladvesi" not in out
    assert "thailandsell" not in out


def test_numeric_facts_normalizes_grouped_numbers():
    a = "Цена 6 900 000 THB, площадь 45,5 м², рассрочка 30%"
    b = "30% · 45.5 м² · 6900000 THB"
    assert phuket_mirror._numeric_facts(a) == phuket_mirror._numeric_facts(b)


def test_relevance_requires_phuket_and_realty_context():
    assert phuket_mirror._looks_relevant("Новый condo в Bang Tao на Пхукете")
    assert not phuket_mirror._looks_relevant("Новости Пхукета: фестиваль на выходных")
    assert not phuket_mirror._looks_relevant("Квартира в Бангкоке от застройщика")
