"""Validation syntaxique des réponses JSON strictes CoFFY, sans dépendance métier."""

import json
import math


def strict_json_value(content):
    """Accepte un document JSON RFC 8259, sans constante non JSON ni clé dupliquée."""
    def no_constant(value):
        raise ValueError(f"Constante JSON interdite : {value}.")

    def no_duplicate(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Clé JSON dupliquée.")
            result[key] = value
        return result

    try:
        value = json.loads(content, parse_constant=no_constant, object_pairs_hook=no_duplicate)
        def check(node):
            if isinstance(node, str):
                node.encode("utf-8")
            elif isinstance(node, float) and not math.isfinite(node):
                raise ValueError("Nombre JSON non fini.")
            elif isinstance(node, list):
                for member in node:
                    check(member)
            elif isinstance(node, dict):
                for key, member in node.items():
                    check(key)
                    check(member)
        check(value)
        return value
    except (TypeError, UnicodeEncodeError, json.JSONDecodeError, RecursionError) as exc:
        raise ValueError("Réponse JSON stricte invalide.") from exc
