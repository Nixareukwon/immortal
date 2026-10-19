"""Nested command templates. Values supplied by chat are always plain text."""

import asyncio
import random
import re
from dataclasses import dataclass


VARIABLE_NAMES = frozenset((
    "accountage", "channel", "chatter", "commands", "countdown", "countup",
    "emote", "followage", "followcount", "fromindex", "horoscope", "index",
    "math", "pick", "query", "randint", "sender", "spam", "subage", "subcount",
    "uptime", "user", "weather",
))


class TemplateError(ValueError):
    pass


class SenderRequired(TemplateError):
    """A timer has no sender and cannot evaluate this template."""


@dataclass(frozen=True)
class Variable:
    name: str
    argument: tuple


def parse_template(template, custom_variable_names=()):
    if len(template) > 4000:
        raise TemplateError("шаблон длиннее 4000 символов")
    known_variables = VARIABLE_NAMES | frozenset(custom_variable_names)
    count = 0

    def parse(text, depth=0):
        nonlocal count
        if depth > 12:
            raise TemplateError("слишком много вложенных переменных (максимум 12)")
        parts = []
        position = 0
        while position < len(text):
            start = text.find("{", position)
            if start < 0:
                parts.append(text[position:])
                break
            parts.append(text[position:start])
            balance = 1
            end = start + 1
            while end < len(text) and balance:
                if text[end] == "{":
                    balance += 1
                elif text[end] == "}":
                    balance -= 1
                end += 1
            if balance:
                parts.append(text[start:])
                break
            content = text[start + 1:end - 1]
            head = re.match(r"([a-z]+)(\d*)(?:\s+|$)", content, re.IGNORECASE)
            name = head[1].casefold() if head else ""
            if head and name in known_variables and (not head[2] or name in ("index", "fromindex")):
                count += 1
                if count > 100:
                    raise TemplateError("слишком много переменных (максимум 100)")
                argument = parse(content[head.end():], depth + 1)
                if head[2]:
                    argument = (head[2] + " ",) + argument
                parts.append(Variable(name, argument))
            else:
                parts.append(text[start:end])
            position = end
        return tuple(parts)

    return parse(str(template))


def needs_sender(parts):
    return any(
        isinstance(part, Variable)
        and (part.name in ("sender", "user") or needs_sender(part.argument))
        for part in parts
    )


def contains_variable(parts, name):
    return any(
        isinstance(part, Variable)
        and (part.name == name or contains_variable(part.argument, name))
        for part in parts
    )


def split_parts(parts, separator):
    """Split literal separators without splitting nested variable arguments."""
    groups = [[]]
    for part in parts:
        if isinstance(part, str):
            chunks = part.split(separator)
            groups[-1].append(chunks[0])
            for chunk in chunks[1:]:
                groups.append([chunk])
        else:
            groups[-1].append(part)
    return groups


async def render_template(template, resolver, *, query="", username="", custom_variable_names=()):
    parts = parse_template(template, custom_variable_names)
    if not username and needs_sender(parts):
        raise SenderRequired("переменная {sender} требует пользователя и недоступна в таймере")

    async def render(nodes):
        output = []
        for node in nodes:
            if isinstance(node, str):
                output.append(node)
                continue
            if node.name == "pick":
                choices = [group for group in split_parts(node.argument, "|") if any(
                    not isinstance(part, str) or part.strip() for part in group
                )]
                value = await render(random.choice(choices)) if choices else "укажи варианты: {pick да|нет}"
            elif node.name == "spam":
                spam_arguments = (await render(node.argument)).strip()
                if not spam_arguments:
                    value = ""
                else:
                    count_text, separator, phrase = spam_arguments.partition(" ")
                    if not separator or not phrase.strip():
                        raise TemplateError(
                            "использование {spam}: укажи количество от 1 до 15 и текст"
                        )
                    try:
                        repeat_count = int(count_text)
                    except ValueError as error:
                        raise TemplateError(
                            "количество в {spam} должно быть числом от 1 до 15"
                        ) from error
                    if not 1 <= repeat_count <= 15:
                        raise TemplateError("количество в {spam} должно быть от 1 до 15")
                    phrase = phrase.strip()
                    value = " ".join([phrase] * repeat_count)
            elif node.name == "query":
                value = query or await render(node.argument)
            elif node.name in ("index", "fromindex"):
                # Keep the fallback lazy: a missing argument is the only reason to evaluate it.
                groups = split_parts(node.argument, " ")
                first = groups.pop(0) if groups else []
                fallback = []
                for group in groups:
                    if fallback:
                        fallback.append(" ")
                    fallback.extend(group)
                try:
                    index = int((await render(first)).strip()) - 1
                    words = query.split()
                    if not 0 <= index < len(words):
                        raise ValueError
                    value = words[index] if node.name == "index" else " ".join(words[index:])
                except ValueError:
                    value = await render(fallback)
            else:
                argument = (await render(node.argument)).strip()
                value = str(await resolver(node.name, argument))
            output.append(value.strip() if node.name == "pick" else value)
        result = "".join(output)
        if len(result) > 4000:
            raise TemplateError("результат длиннее 4000 символов")
        return result

    try:
        async with asyncio.timeout(30):
            return await render(parts)
    except TimeoutError as error:
        raise TemplateError("истекло время получения данных переменных; попробуй позже") from error
