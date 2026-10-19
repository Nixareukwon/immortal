"""Offline regression tests for documented command templates and bot integration."""

import importlib
import os
import unittest
from collections import deque
from datetime import datetime, timedelta
from types import MethodType, SimpleNamespace
from unittest.mock import AsyncMock, patch

from command_variables import SenderRequired, TemplateError, render_template


# Import the command definitions without reading .env, connecting to Twitch, or
# constructing Bot (whose constructor opens the live database).
with patch("dotenv.load_dotenv"), patch.dict(os.environ, {}, clear=True):
    bot_module = importlib.import_module("bot")


class TemplateTests(unittest.IsolatedAsyncioTestCase):
    async def test_query_and_resolver_values_are_never_reparsed(self):
        resolver = AsyncMock(return_value="{math 2+2}")
        result = await render_template(
            "{query}|{channel}", resolver, query="{sender}", username="viewer"
        )
        self.assertEqual(result, "{sender}|{math 2+2}")
        resolver.assert_awaited_once_with("channel", "")

    async def test_unknown_and_unclosed_variables_stay_literal(self):
        resolver = AsyncMock()
        template = "{unknown {sender}} {hello} text {query unfinished"
        self.assertEqual(await render_template(template, resolver), template)
        resolver.assert_not_awaited()

    async def test_custom_variable_names_can_be_enabled_per_template(self):
        resolver = AsyncMock(return_value="custom value")
        self.assertEqual(
            await render_template(
                "Привет, {greeting}!", resolver, custom_variable_names={"greeting"}
            ),
            "Привет, custom value!",
        )
        resolver.assert_awaited_once_with("greeting", "")
        self.assertEqual(await render_template("{greeting}", resolver), "{greeting}")

    async def test_query_fallback_is_lazy(self):
        resolver = AsyncMock(return_value="fallback")
        result = await render_template(
            "{query {weather Riga}}", resolver, query="Paris", username="viewer"
        )
        self.assertEqual(result, "Paris")
        resolver.assert_not_awaited()
        self.assertEqual(
            await render_template("{query {weather Riga}}", resolver), "fallback"
        )
        resolver.assert_awaited_once_with("weather", "Riga")

    async def test_spam_marker_is_removed_from_rendered_response(self):
        resolver = AsyncMock()
        result = await render_template("Тг канал https://t.me/example {spam}", resolver)
        self.assertEqual(result, "Тг канал https://t.me/example ")
        resolver.assert_not_awaited()

    async def test_spam_variable_repeats_phrase_like_fossabot(self):
        resolver = AsyncMock()
        result = await render_template(
            "{spam 3 Тг канал https://t.me/example}", resolver
        )
        self.assertEqual(
            result,
            "Тг канал https://t.me/example "
            "Тг канал https://t.me/example "
            "Тг канал https://t.me/example",
        )
        resolver.assert_not_awaited()

    async def test_spam_variable_resolves_nested_phrase_variables(self):
        resolver = AsyncMock(return_value="https://t.me/example")
        result = await render_template(
            "{spam 2 {query}}", resolver, query="https://t.me/example"
        )
        self.assertEqual(
            result, "https://t.me/example https://t.me/example"
        )
        resolver.assert_not_awaited()

    async def test_spam_variable_rejects_invalid_count_or_missing_phrase(self):
        resolver = AsyncMock()
        for template in (
            "{spam 0 hello}",
            "{spam 16 hello}",
            "{spam many hello}",
            "{spam 3}",
        ):
            with self.subTest(template=template), self.assertRaises(TemplateError):
                await render_template(template, resolver)

    async def test_pick_resolves_only_the_selected_nested_choice(self):
        resolver = AsyncMock(return_value="selected")
        with patch("command_variables.random.choice", side_effect=lambda choices: choices[0]):
            result = await render_template(
                "{pick {channel}|{weather Riga}}", resolver, username="viewer"
            )
        self.assertEqual(result, "selected")
        resolver.assert_awaited_once_with("channel", "")

    async def test_both_index_syntaxes_and_lazy_fallbacks(self):
        resolver = AsyncMock(return_value="fallback")
        for variable in ("index2", "index 2"):
            with self.subTest(variable=variable):
                result = await render_template(
                    "{" + variable + " {weather Riga}}", resolver, query="one two three"
                )
                self.assertEqual(result, "two")
        for variable in ("fromindex2", "fromindex 2"):
            with self.subTest(variable=variable):
                result = await render_template(
                    "{" + variable + " {weather Riga}}", resolver, query="one two three"
                )
                self.assertEqual(result, "two three")
        resolver.assert_not_awaited()
        self.assertEqual(
            await render_template("{index3 {weather Riga}}", resolver, query="one"),
            "fallback",
        )
        resolver.assert_awaited_once_with("weather", "Riga")

    async def test_nested_sender_is_unavailable_without_a_timer_sender(self):
        resolver = AsyncMock()
        with self.assertRaises(SenderRequired):
            await render_template("{query {sender}}", resolver, username=None)
        resolver.assert_not_awaited()

    async def test_template_and_result_limits(self):
        resolver = AsyncMock(return_value="x" * 4001)
        for template in ("x" * 4001, "{channel}" * 101, "{math " * 14 + "1" + "}" * 14):
            with self.subTest(template=template[:35]), self.assertRaises(TemplateError):
                await render_template(template, resolver)
        with self.assertRaises(TemplateError):
            await render_template("{channel}", resolver)


class BotVariablesTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.db = bot_module.Database(":memory:")
        self.addCleanup(self.db.conn.close)
        self.channel = SimpleNamespace(name="streamer", send=AsyncMock(), chatters=[])
        self.bot = SimpleNamespace(
            db=self.db,
            nick="robot",
            _commands={"help": SimpleNamespace(name="commands")},
            custom_command_uses={},
            refresh_banned_words=lambda: None,
            repeat_uses={},
            active={},
            recent_messages=deque(),
            mass_actions={},
            custom_banned_words={},
            banned_words_pattern=lambda channel: None,
            get_command=lambda name: None,
            handle_antiflood=AsyncMock(return_value=False),
            check_moderation=lambda message: None,
            scopes=set(),
            weather_service=SimpleNamespace(get_weather=AsyncMock(return_value="Погода: ясно, +12°C")),
            variable_services=SimpleNamespace(resolve=AsyncMock(return_value="Дева: прогноз на сегодня")),
        )
        for name in (
            "resolve_custom_variable", "render_custom_response", "process_command_message",
            "check_repeat_cooldown", "event_message", "send_timer_message", "get_weather",
        ):
            method = getattr(bot_module.Bot, name, None)
            if method is not None:
                setattr(self.bot, name, MethodType(method, self.bot))
        self.bot.calculate_math = bot_module.Bot.calculate_math
        self.bot.parse_variable_datetime = bot_module.Bot.parse_variable_datetime
        self.bot.format_elapsed = bot_module.Bot.format_elapsed
        self.bot.send_variable_response = bot_module.Bot.send_variable_response

    async def render(self, template, query="", username="viewer"):
        return await self.bot.render_custom_response(
            template, username=username, channel_name="streamer", query=query, channel=self.channel
        )

    def message(self, content, *, moderator=False):
        return SimpleNamespace(
            content=content,
            channel=self.channel,
            author=SimpleNamespace(name="viewer", is_mod=moderator, is_broadcaster=False),
            echo=False,
        )

    async def test_nested_math_random_and_query(self):
        with patch("bot.random.randint", return_value=3):
            self.assertEqual(await self.render("{math {randint 1 5}+{query 0}}", "4"), "7")
        self.assertEqual(await self.render("{math {query 0}+2}"), "2")

    async def test_custom_variable_value_is_used_in_command_and_timer_templates(self):
        self.db.conn.execute(
            "INSERT INTO custom_variables(channel,name,value,description) VALUES(?,?,?,?)",
            ("streamer", "greeting", "Привет из переменной", "Текст приветствия"),
        )
        self.db.conn.commit()
        self.assertEqual(await self.render("Начало: {greeting}"), "Начало: Привет из переменной")
        await self.bot.send_timer_message(self.channel, "{greeting}")
        self.channel.send.assert_awaited_once_with("Привет из переменной")

    async def test_weather_forwards_nested_city_queries_without_reparsing_input(self):
        template = "{weather {query Рига}}"
        for city in ("New York, US", "{sender} Latvia"):
            with self.subTest(city=city):
                self.bot.weather_service.get_weather.reset_mock()
                self.assertEqual(await self.render(template, city), "Погода: ясно, +12°C")
                self.bot.weather_service.get_weather.assert_awaited_once_with(city)

    async def test_weather_timer_uses_the_same_nested_template_city_fallback(self):
        self.assertTrue(await self.bot.send_timer_message(self.channel, "{weather {query Рига}}"))
        self.bot.weather_service.get_weather.assert_awaited_once_with("Рига")
        self.channel.send.assert_awaited_once_with("Погода: ясно, +12°C")

    async def test_saved_weather_command_uses_requested_city_ahead_of_fixed_city(self):
        self.db.set_custom("streamer", "weather", "Погода: {weather Москва}")
        self.bot.weather_service.get_weather.return_value = "Казань: ясно, +12°C"
        await self.bot.process_command_message(
            self.message("!weather Казань"), "streamer", "viewer", 1
        )
        self.bot.weather_service.get_weather.assert_awaited_once_with("Казань")
        self.channel.send.assert_awaited_once_with("Погода: Казань: ясно, +12°C")

    async def test_bare_weather_accepts_a_whole_city_query(self):
        await self.render("{weather}", "Нижний Новгород, RU")
        self.bot.weather_service.get_weather.assert_awaited_once_with("Нижний Новгород, RU")

    async def test_horoscope_query_overrides_fixed_sign_and_timer_keeps_fallback(self):
        await self.render("{horoscope овен}", "дева")
        self.bot.variable_services.resolve.assert_awaited_once_with(
            "horoscope", "дева", username="viewer", channel_name="streamer", tags=None
        )
        self.bot.variable_services.resolve.reset_mock()
        await self.bot.send_timer_message(self.channel, "{horoscope овен}")
        self.bot.variable_services.resolve.assert_awaited_once_with(
            "horoscope", "овен", username="", channel_name="streamer", tags=None
        )

    async def test_saved_bare_horoscope_accepts_sign_and_alias_respects_cooldown(self):
        self.db.set_custom("streamer", "гороскоп", "{horoscope}", cooldown_seconds=15)
        self.db.set_custom_aliases("streamer", "гороскоп", ["горо"])
        with patch("bot.time.time", return_value=1000):
            await self.bot.process_command_message(
                self.message("!гороскоп дева"), "streamer", "viewer", 1
            )
            self.channel.send.assert_awaited_once_with("Дева: прогноз на сегодня")
            await self.bot.process_command_message(
                self.message("!горо овен"), "streamer", "viewer", 1
            )
        self.bot.variable_services.resolve.assert_awaited_once_with(
            "horoscope", "дева", username="viewer", channel_name="streamer", tags=None
        )
        self.assertIn("через 15 сек.", self.channel.send.await_args.args[0])

    async def test_bare_horoscope_without_sign_explains_usage_without_provider(self):
        self.bot.variable_services = bot_module.VariableServices(self.bot)
        with patch.object(self.bot.variable_services, "_horoscope_feed", new_callable=AsyncMock) as feed:
            result = await self.render("{horoscope}")
        self.assertIn("укажи знак зодиака", result)
        feed.assert_not_awaited()

    async def test_math_rejects_python_calls_and_extreme_powers(self):
        for expression in ("__import__('os').getcwd()", "2**100000000", "1/0"):
            with self.subTest(expression=expression):
                self.assertIn("ошиб", (await self.render("{math " + expression + "}")).lower())

    async def test_public_numeric_query_is_not_a_repeat_request(self):
        self.db.set_custom("streamer", "number", "Ответ: {query}")
        for number in ("2", "42"):
            with self.subTest(number=number):
                self.channel.send.reset_mock()
                message = self.message("!number " + number)
                await self.bot.process_command_message(message, "streamer", "viewer", 1)
                self.channel.send.assert_awaited_once_with("Ответ: " + number)

    async def test_public_user_cannot_request_repeats(self):
        self.db.set_custom("streamer", "hello", "Привет")
        with patch("bot.DEBUG", False):
            await self.bot.event_message(self.message("!hello x2"))
        self.channel.send.assert_awaited_once()
        self.assertIn("только модераторы", self.channel.send.await_args.args[0])

    async def test_moderator_can_repeat_static_command_with_numeric_suffix(self):
        response = "Тг канал https://t.me/smailrofltg"
        self.db.set_custom("streamer", "tg", response)
        message = self.message("!tg 15", moderator=True)

        with patch("bot.DEBUG", False), patch("bot.asyncio.sleep", new_callable=AsyncMock) as sleep:
            await self.bot.event_message(message)

        self.assertEqual(self.channel.send.await_count, 15)
        sleep.assert_not_awaited()
        self.assertTrue(
            all(call.args == (response,) for call in self.channel.send.await_args_list)
        )
        self.assertEqual(message.content, "!tg 15")

    async def test_custom_spam_variable_repeats_response_up_to_requested_count(self):
        response = "Тг канал https://t.me/example{spam}"
        self.db.set_custom("streamer", "tg", response)
        message = self.message("!tg 3", moderator=True)

        with patch("bot.DEBUG", False), patch(
            "bot.asyncio.sleep", new_callable=AsyncMock
        ) as sleep:
            await self.bot.event_message(message)

        self.assertEqual(
            [call.args[0] for call in self.channel.send.await_args_list],
            ["Тг канал https://t.me/example"] * 3,
        )
        sleep.assert_not_awaited()

    async def test_custom_command_can_use_fossabot_style_spam_variable(self):
        self.db.set_custom("streamer", "tgline", "{spam 3 Тг канал https://t.me/example}")

        await self.bot.process_command_message(
            self.message("!tgline"), "streamer", "viewer", 1
        )

        self.channel.send.assert_awaited_once_with(
            "Тг канал https://t.me/example "
            "Тг канал https://t.me/example "
            "Тг канал https://t.me/example"
        )

    async def test_custom_spam_variable_preserves_query_before_repeat_count(self):
        self.db.set_custom("streamer", "tgpromo", "Ссылка: {query}{spam}")

        with patch("bot.DEBUG", False), patch(
            "bot.asyncio.sleep", new_callable=AsyncMock
        ):
            await self.bot.event_message(
                self.message("!tgpromo https://t.me/example 2", moderator=True)
            )

        self.assertEqual(
            [call.args[0] for call in self.channel.send.await_args_list],
            ["Ссылка: https://t.me/example"] * 2,
        )

    async def test_custom_spam_variable_rejects_count_over_limit(self):
        self.db.set_custom("streamer", "tg", "Тг канал {spam}")

        with patch("bot.DEBUG", False):
            await self.bot.event_message(self.message("!tg 16", moderator=True))

        self.channel.send.assert_awaited_once_with(
            "Количество повторов должно быть от 1 до 15."
        )

    async def test_custom_spam_variable_rejects_repeat_from_non_moderator(self):
        self.db.set_custom("streamer", "tg", "Тг канал {spam}")

        with patch("bot.DEBUG", False):
            await self.bot.event_message(self.message("!tg 3"))

        self.channel.send.assert_awaited_once()
        self.assertIn("только модераторы", self.channel.send.await_args.args[0])

    async def test_moderator_can_repeat_builtin_command_with_numeric_suffix(self):
        callback = AsyncMock()
        command = SimpleNamespace(
            params={"self": object(), "ctx": object()},
            _instance=self.bot,
            _callback=callback,
        )
        context = SimpleNamespace(
            command=command,
            args=[],
            kwargs={},
        )
        self.bot.get_command = lambda name: command if name == "tg" else None
        self.bot.get_context = AsyncMock(return_value=context)

        async def invoke(ctx):
            await ctx.command._callback(ctx)

        self.bot.invoke = AsyncMock(side_effect=invoke)
        self.bot.check_repeat_cooldown = AsyncMock(return_value=True)

        with patch("bot.asyncio.sleep", new_callable=AsyncMock) as sleep:
            await self.bot.process_command_message(
                self.message("!tg 15", moderator=True), "streamer", "viewer", 1
            )

        self.assertEqual(callback.await_count, 15)
        sleep.assert_not_awaited()
        self.assertEqual(self.bot.invoke.await_count, 1)
        self.assertEqual(self.bot.get_context.await_args.args[0].content, "!tg 15")

    async def test_twitchio_parses_builtin_repeat_as_count(self):
        real_bot = bot_module.Bot("oauth:dummy", "dummy", "0", [], db_path=":memory:")
        self.addAsyncCleanup(real_bot.close)
        command = real_bot.get_command("daily")
        command._callback = AsyncMock()
        channel = SimpleNamespace(name="streamer", send=AsyncMock(), chatters=[])
        author = SimpleNamespace(
            name="moderator", is_mod=True, is_broadcaster=False, _ws=object()
        )
        message = SimpleNamespace(
            content="!daily 15", tags={}, channel=channel, author=author, echo=False
        )

        with patch("bot.asyncio.sleep", new_callable=AsyncMock) as sleep:
            await real_bot.process_command_message(
                message, "streamer", "moderator", 1
            )

        self.assertEqual(command._callback.await_count, 15)
        sleep.assert_not_awaited()
        self.assertEqual(command._callback.await_args.args[-1].args, [])

    async def test_numeric_argument_of_builtin_command_is_not_repeat_count(self):
        callback = AsyncMock()
        command = SimpleNamespace(
            params={"self": object(), "ctx": object(), "sides": object()},
            _instance=self.bot,
            _callback=callback,
        )
        context = SimpleNamespace(
            command=command,
            args=[15],
            kwargs={},
        )
        self.bot.get_command = lambda name: command if name == "dice" else None
        self.bot.get_context = AsyncMock(return_value=context)
        self.bot.invoke = AsyncMock()
        self.bot.check_repeat_cooldown = AsyncMock(return_value=True)

        with patch("bot.asyncio.sleep", new_callable=AsyncMock) as sleep:
            await self.bot.process_command_message(
                self.message("!dice 15", moderator=True), "streamer", "viewer", 1
            )
            self.bot.invoke.assert_awaited_once_with(context)
            callback.assert_not_awaited()
            sleep.assert_not_awaited()

    async def test_numeric_suffix_remains_query_when_custom_command_uses_query(self):
        self.db.set_custom("streamer", "number", "Ответ: {query}")
        with patch("bot.DEBUG", False):
            await self.bot.event_message(self.message("!number 15", moderator=True))
        self.channel.send.assert_awaited_once_with("Ответ: 15")

    async def test_documented_random_query_accepts_two_public_numeric_arguments(self):
        self.db.set_custom("streamer", "random", "Число: {randint {query 0 100}}")
        with patch("bot.random.randint", return_value=37) as randint, patch("bot.DEBUG", False):
            await self.bot.event_message(self.message("!random 20 50"))
        randint.assert_called_once_with(20, 50)
        self.channel.send.assert_awaited_once_with("Число: 37")

    async def test_numbered_chatters_are_stable_distinct_and_channel_specific(self):
        self.channel.chatters = [
            SimpleNamespace(name="alice"), SimpleNamespace(name="bob"),
            SimpleNamespace(name="ROBOT"),
        ]
        self.bot.recent_messages.extend((
            ("streamer", "alice", "hello", False),
            ("streamer", "charlie", "hello", False),
            ("streamer", "robot", "hello", False),
            ("another_channel", "outsider", "hello", False),
        ))
        with patch("bot.random.sample", side_effect=lambda users, count: list(users)):
            result = await self.render("{chatter 1}|{chatter 1}|{chatter 2}|{chatter 3}")
        first, repeated, second, third = result.split("|")
        self.assertEqual(first, repeated)
        self.assertEqual({first, second, third}, {"alice", "bob", "charlie"})

    async def test_alias_repeat_and_cooldown_share_canonical_command(self):
        self.db.set_custom("streamer", "hello", "Привет, {query}", cooldown_seconds=30)
        self.db.set_custom_aliases("streamer", "hello", ["hi"])
        message = self.message("!hi friends x2", moderator=True)
        with patch("bot.DEBUG", False), patch("bot.time.time", return_value=1000):
            await self.bot.event_message(message)
            self.assertEqual(self.channel.send.await_count, 2)
            self.assertEqual([call.args[0] for call in self.channel.send.await_args_list],
                             ["Привет, friends", "Привет, friends"])
            self.assertEqual(message.content, "!hi friends x2")
            await self.bot.process_command_message(
                self.message("!hello again"), "streamer", "viewer", 1
            )
        self.assertEqual(self.channel.send.await_count, 3)
        self.assertIn("через 30 сек.", self.channel.send.await_args.args[0])

    async def test_timer_renders_variables_and_skips_sender_templates(self):
        self.assertTrue(await self.bot.send_timer_message(self.channel, "{channel}: {math 2+3}"))
        self.channel.send.assert_awaited_once_with("streamer: 5")
        self.assertFalse(await self.bot.send_timer_message(self.channel, "Привет, {sender}"))
        self.assertFalse(await self.bot.send_timer_message(self.channel, "{query {user}}"))
        self.assertFalse(await self.bot.send_timer_message(self.channel, "ТГ {spam}"))
        self.channel.send.assert_awaited_once()

    async def test_large_command_lists_are_sent_without_exceeding_chat_limit(self):
        text = " ".join("!command" + str(index) for index in range(100))
        await self.bot.send_variable_response(self.channel, text)
        messages = [call.args[0] for call in self.channel.send.await_args_list]
        self.assertGreater(len(messages), 1)
        self.assertTrue(all(len(message) <= 450 for message in messages))
        self.assertEqual(" ".join(messages), text)

    async def test_commands_filters_custom_commands(self):
        self.db.set_custom("streamer", "zebra", "one")
        self.db.set_custom("streamer", "alpha", "two")
        public = await self.render("{commands public}")
        self.assertIn("!alpha", public)
        self.assertIn("!zebra", public)
        self.assertNotIn("!commands", public)
        for scope in ("private", "offline"):
            with self.subTest(scope=scope):
                result = await self.render("{commands " + scope + "}")
                self.assertNotIn("!alpha", result)
                self.assertNotIn("!zebra", result)

    def test_documented_date_formats(self):
        dates = (
            ("2030-01-03", datetime(2030, 1, 3)),
            ("2030-01-03T23:30:45Z", datetime(2030, 1, 3, 23, 30, 45)),
            ("2030.01.03", datetime(2030, 1, 3)),
            ("Jan 3rd 2030", datetime(2030, 1, 3)),
            ("January 3rd 2030", datetime(2030, 1, 3)),
            ("3rd Jan 2030", datetime(2030, 1, 3)),
            ("3rd January 2030", datetime(2030, 1, 3)),
            ("Jan 3rd 2030 11:30pm", datetime(2030, 1, 3, 23, 30)),
            ("Jan 3rd 2030 12:00am", datetime(2030, 1, 3, 0, 0)),
            ("Jan 3rd 2030 12:00pm", datetime(2030, 1, 3, 12, 0)),
            ("3rd January 2030 11:30:45pm +03:00", datetime(2030, 1, 3, 23, 30, 45)),
        )
        for value, expected in dates:
            with self.subTest(value=value):
                result = bot_module.Bot.parse_variable_datetime(value)
                self.assertEqual(result.replace(tzinfo=None), expected)
                if value.endswith("+03:00"):
                    self.assertEqual(result.utcoffset(), timedelta(hours=3))


if __name__ == "__main__":
    unittest.main()
