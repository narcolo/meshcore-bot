#!/usr/bin/env python3
"""
Fact command for the MeshCore Bot
Provides random interesting facts from various categories
"""

import json
import logging
import os
import random
from typing import Any, Dict, List, Optional
from .base_command import BaseCommand
from ..models import MeshMessage


class FactCommand(BaseCommand):
    """Handles fact commands with category support and shuffle-bag randomness."""

    # Plugin metadata
    name = "fact"
    keywords = ['fact', 'facts']
    description = "Get a random interesting fact (usage: fact [category])"
    category = "fun"
    cooldown_seconds = 3
    requires_dm = False
    requires_internet = False

    # Documentation
    short_description = "Get a random interesting fact"
    usage = "fact [category]"
    examples = ["fact", "fact radio", "fact nature"]
    parameters = [
        {"name": "category", "description": "radio, computers, nature, poland, geography (optional)"}
    ]

    # Category name mappings (aliases -> canonical category key)
    CATEGORY_ALIASES = {
        'radio': 'radio',
        'computers': 'computers',
        'computer': 'computers',
        'komputery': 'computers',
        'komputer': 'computers',
        'nature': 'nature',
        'natura': 'nature',
        'przyroda': 'nature',
        'poland': 'poland',
        'polska': 'poland',
        'geography': 'geography',
        'geografia': 'geography',
        'geo': 'geography',
        'swiat': 'geography',
    }

    CATEGORIES = ['radio', 'computers', 'nature', 'poland', 'geography']

    def __init__(self, bot: Any):
        super().__init__(bot)
        self.fact_enabled = self.get_config_value('Fact_Command', 'enabled', fallback=True, value_type='bool')

        # Load facts from translation file
        self.facts: Dict[str, List[str]] = {}
        self._load_facts()

        # Shuffle bags for no-repeat rotation (keyed by category name or 'all')
        self._fact_bags: Dict[str, List[str]] = {}

    def _load_facts(self):
        """Load facts from a language-specific JSON file."""
        lang = getattr(getattr(self.bot, 'translator', None), 'language', 'en')
        base_lang = lang.split('-')[0] if '-' in lang else lang
        translation_path = self.get_config_value('Localization', 'translation_path', fallback='translations/')

        for try_lang in [lang, base_lang]:
            facts_file = os.path.join(translation_path, f'facts_{try_lang}.json')
            if os.path.isfile(facts_file):
                try:
                    with open(facts_file, 'r', encoding='utf-8') as f:
                        self.facts = json.load(f)
                    self.logger.info(f"Loaded facts from {facts_file}")
                    return
                except Exception as e:
                    self.logger.error(f"Error loading facts from {facts_file}: {e}")

        # Fallback: empty dict means no facts available
        if not self.facts:
            self.logger.warning("No facts file found for any language")

    def _get_fact(self, category: Optional[str] = None) -> Optional[str]:
        """Get a fact using shuffle-bag so all facts are shown before repeats.

        Args:
            category: Canonical category key, or None for any category.

        Returns:
            A fact string, or None if no facts available.
        """
        if not self.facts:
            return None

        bag_key = category or 'all'

        # Refill the bag when empty
        if not self._fact_bags.get(bag_key):
            pool = []
            if category:
                pool = list(self.facts.get(category, []))
            else:
                for cat_facts in self.facts.values():
                    pool.extend(cat_facts)
            if not pool:
                return None
            random.shuffle(pool)
            self._fact_bags[bag_key] = pool

        return self._fact_bags[bag_key].pop()

    def _get_category_display_names(self) -> str:
        """Get comma-separated list of available category names."""
        # Try translated category names first
        translated = self.translate_get_value('commands.fact.categories')
        if translated and isinstance(translated, dict):
            return ', '.join(translated.values())
        return ', '.join(self.CATEGORIES)

    def can_execute(self, message: MeshMessage) -> bool:
        if not self.fact_enabled:
            return False
        return super().can_execute(message)

    async def execute(self, message: MeshMessage) -> bool:
        content = message.content.strip()
        if content.startswith('!'):
            content = content[1:].strip()

        parts = content.split()
        category = None

        if len(parts) >= 2:
            category_input = parts[1].lower()
            category = self.CATEGORY_ALIASES.get(category_input)

            if category is None:
                # Invalid category - show available ones
                available = self._get_category_display_names()
                no_cat_msg = self.translate(
                    'commands.fact.no_category',
                    categories=available
                )
                # Fallback if translation key not found
                if no_cat_msg.startswith('commands.fact.'):
                    no_cat_msg = f"Unknown category. Available: {available}"
                await self.send_response(message, no_cat_msg)
                return True

        try:
            self.record_execution(message.sender_id)

            fact = self._get_fact(category)
            if fact:
                await self.send_response(message, fact)
            else:
                error_msg = self.translate('commands.fact.error')
                if error_msg.startswith('commands.fact.'):
                    error_msg = "No facts available!"
                await self.send_response(message, error_msg)
            return True

        except Exception as e:
            self.logger.error(f"Error in fact command: {e}")
            error_msg = self.translate('commands.fact.error')
            if error_msg.startswith('commands.fact.'):
                error_msg = "Error getting fact!"
            await self.send_response(message, error_msg)
            return True
