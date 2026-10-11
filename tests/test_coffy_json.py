"""Contrat JSON strict CoFFY, indépendant du fournisseur LLM."""

import unittest

from app.coffy_json import strict_json_value


class StrictJSONTests(unittest.TestCase):
    def test_objet_tableau_et_scalaire_valides(self):
        self.assertEqual(strict_json_value('{"items":[1,true,null],"text":"é"}')["items"], [1, True, None])
        self.assertEqual(strict_json_value('"réponse"'), "réponse")

    def test_texte_libre_markdown_et_json_partiel_refuses(self):
        for value in ("texte libre", '```json\n{"ok":true}\n```', '{"ok":true} suite', ""):
            with self.subTest(value=value), self.assertRaises(ValueError):
                strict_json_value(value)

    def test_constantes_cles_dupliquees_et_unicode_invalides_refuses(self):
        for value in ('{"x":NaN}', '{"x":1,"x":2}', '{"x":1e999}', '"\\ud800"'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                strict_json_value(value)

    def test_profondeur_excessive_refusee(self):
        with self.assertRaises(ValueError):
            strict_json_value("[" * 2000 + "0" + "]" * 2000)


if __name__ == "__main__":
    unittest.main()
