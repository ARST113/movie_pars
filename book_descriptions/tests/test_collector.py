import os
import sys
import unittest
from unittest.mock import Mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from collector import BookRow, group_shard, is_product_row, key_for, pick_columns, reuse, letter
from extraction import from_html, from_markdown, normalise_text, plausible, trusted_url


class CollectorTests(unittest.TestCase):
    def test_pick_column_variants(self):
        self.assertEqual(pick_columns(['Title', 'Описание книги (по издателю)', 'Источник описания']), (1, 2))
        self.assertEqual(pick_columns(['Title', 'Сюжетная аннотация (по карточке книги)', 'Источник аннотации']), (1, 2))
        self.assertEqual(pick_columns(['Title', 'Описание / проверка жанра', 'Источник классификации']), (None, None))

    def test_key_author_permutation(self):
        self.assertEqual(key_for('Безмолвный пациент', 'Михаэлидес Алекс'), key_for('Безмолвный пациент', 'Алекс Михаэлидес'))

    def test_book_row_not_placeholders(self):
        self.assertFalse(is_product_row(['—', 'Рейтинг не подтверждён', '', '', '', '', '', '', 'https://www.ozon.ru/category/knigi-16500/']))
        self.assertTrue(is_product_row(['1', 'Пламя одержимости', 'Майк Омер', '', '', '', '', '', 'https://book24.ru/product/plamya-oderzhimosti-6769433/']))

    def test_security_urls(self):
        self.assertTrue(trusted_url('https://www.labirint.ru/books/200/'))
        self.assertFalse(trusted_url('http://labirint.ru/books/200/'))
        self.assertFalse(trusted_url('https://labirint.ru.evil.com/books/200/'))

    def test_schema_org(self):
        html = '''<script type="application/ld+json">{"@type":"Book","description":"Первый герой попадает в странный город, где неожиданно пропадают люди. Вместе со спутницей он ищет виновного, разгадывает тайну и противостоит скрытой угрозе."}</script>'''
        self.assertIn('Первый герой', from_html(html))

    def test_generic_meta_rejected(self):
        html = '<meta name="description" content="Купить книгу в интернет-магазине книг со скидкой! Доставка по всей России, отзывы покупателей, большой ассортимент изданий и удобный сервис.">'
        self.assertIsNone(from_html(html))

    def test_labirint_seo_snippet_rejected(self):
        snippet = ('Книга: Пламя одержимости (A Burning Obsession).📙 Автор: Майк Омер. '
                   'Аннотация, 🔝 отзывы читателей, иллюстрации. Купить книгу по привлекательной цене '
                   'среди миллиона книг "Лабиринта" | ISBN 978-5-04-185552-9')
        html = '<meta name="description" content="' + snippet.replace('"', '&quot;') + '">'
        self.assertFalse(plausible(snippet))
        self.assertIsNone(from_html(html))

    def test_ast_seo_description_rejected(self):
        snippet = ('Суть вещи» Алексина Алена, Русский Corpus. Книги издательства АСТ. '
                   'Аннотация, характеристики, фото обложки, информация, где купить книгу.')
        self.assertFalse(plausible(snippet))
        self.assertIsNone(from_html('<meta name="description" content="' + snippet + '">'))

    def test_storefront_marketing_excerpt_rejected(self):
        excerpt = ('Полистать От автора супербестселлера Внутри убийцы. '
                   'Первый роман из нового цикла. Лейтенант полиции Нью-Йорка '
                   'Эбби Маллен ведёт расследование необычного похищения.')
        self.assertFalse(plausible(excerpt))

    def test_chitai_gorod_catalog_prefix_removed(self):
        text = ('Описание и характеристики Young Adult Игры на выживание Читайте и помогайте! '
                '«Последний гамбит» — детективный роман о девушке, которой необходимо '
                'раскрыть тайны дома Хоторнов до совершеннолетия, спасая наследство.')
        cleaned = normalise_text(text)
        self.assertTrue(cleaned.startswith('«Последний гамбит» —'))
        self.assertTrue(plausible(cleaned))

    def test_markdown_section(self):
        markdown = '# Название\n## О книге\nГероиня оказывается вдали от дома, вынуждена искать своего пропавшего брата и исследовать тайны старого королевства. На её пути встречаются друзья, враги и неожиданное предательство.\n## Характеристики\nISBN 123'
        self.assertIn('королевства', from_markdown(markdown))

    def test_labirint_real_annotation_with_preview_controls(self):
        md = ("# Аленький цветочек\n## Аннотация\n"
              "Полистать Содержание\n"
              "«Аленький цветочек» — сказка Сергея Аксакова о младшей дочери купца, "
              "которая отправляется жить к заколдованному чудищу и открывает "
              "тайну его превращения. Девушке предстоит сделать выбор и спасти "
              "того, кого она полюбила, преодолев страх.\n"
              "Полистать Содержание\n"
              "## Характеристики\nISBN 9785000000001")
        result = from_markdown(md)
        self.assertIsNotNone(result)
        self.assertTrue(result.startswith("«Аленький цветочек»"))
        self.assertNotIn("Полистать", result)
        self.assertNotIn("Содержание", result)

    def test_labirint_collapsed_preview_controls_at_end(self):
        md = ("## Аннотация\nПолистать\nСодержание\n"
              "Героиня расследует загадочные исчезновения людей в городе, "
              "где каждый дом хранит собственные тайны. Собирая свидетельства "
              "и разгадывая письма, она обнаруживает, что за преступлениями "
              "стоит тайное общество, связанное с её прошлым.\n"
              "Свернуть !Развернуть Полистать Содержание\n"
              "## Характеристики\nОтзывы и цена")
        result = from_markdown(md)
        self.assertIsNotNone(result)
        self.assertTrue(result.startswith("Героиня расследует"))
        self.assertNotIn("Свернуть", result)
        self.assertNotIn("Полистать", result)
        self.assertNotIn("Содержание", result)

    def test_labirint_controls_without_synopsis_still_rejected(self):
        md = ("## Аннотация\nПолистать Содержание\n"
              "Купить книгу в интернет-магазине с доставкой по всей России. "
              "Читайте отзывы покупателей и выбирайте книги по выгодной цене.\n"
              "## Характеристики\nISBN 9785000000001")
        self.assertIsNone(from_markdown(md))

    def test_underlined_eksmo_description(self):
        md = 'Люди за спиной\n==================\nО книге Читать фрагмент\n------------------\nАнастасия Каменская ведёт расследование исчезновения женщины, о которой заказчик почти ничего не знает. По мере поисков детектив обнаруживает поддельные документы и сложную историю с несколькими смертями.\n\nЧитать полностью\n'
        self.assertIn('Каменская', from_markdown(md))

    def test_shards(self):
        rows = [BookRow('Эксмо', 10, n, ['1', f'Книга {n}', 'Тестов Автор', '', '', '', '', '', f'https://eksmo.ru/book/a-{n}/'], 17, 18) for n in range(2, 200)]
        a = group_shard(rows, 0, 8, 0, 10000)
        b = group_shard(rows, 1, 8, 0, 10000)
        self.assertEqual(set(map(id, a)) & set(map(id, b)), set())
        self.assertTrue(a)

    def test_columns_letter(self):
        self.assertEqual(letter(0), 'A')
        self.assertEqual(letter(18), 'S')
        self.assertEqual(letter(26), 'AA')


if __name__ == '__main__':
    unittest.main()
