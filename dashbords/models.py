from __future__ import annotations

from django.db import models


class Deck(models.Model):
    """
    使用デッキのマスタ（ユーザーごと）
    """

    user = models.ForeignKey(
        "auth.User",
        on_delete=models.CASCADE,
        related_name="decks",
        verbose_name="ユーザー",
    )
    name = models.CharField("デッキ名", max_length=100)
    is_active = models.BooleanField("有効", default=True)
    created_at = models.DateTimeField("作成日時", auto_now_add=True)
    updated_at = models.DateTimeField("更新日時", auto_now=True)

    class Meta:
        verbose_name = "Deck"
        verbose_name_plural = "Decks"
        constraints = [
            models.UniqueConstraint(fields=["user", "name"], name="uniq_deck_per_user"),
        ]
        ordering = ["name", "id"]

    def __str__(self) -> str:
        return self.name


class OpponentDeck(models.Model):
    """
    対面デッキのマスタ（ユーザーごと）
    """

    user = models.ForeignKey(
        "auth.User",
        on_delete=models.CASCADE,
        related_name="opponent_decks",
        verbose_name="ユーザー",
    )
    name = models.CharField("デッキ名", max_length=100)
    is_active = models.BooleanField("有効", default=True)
    created_at = models.DateTimeField("作成日時", auto_now_add=True)
    updated_at = models.DateTimeField("更新日時", auto_now=True)

    class Meta:
        verbose_name = "OpponentDeck"
        verbose_name_plural = "OpponentDecks"
        constraints = [
            models.UniqueConstraint(fields=["user", "name"], name="uniq_opponent_deck_per_user"),
        ]
        ordering = ["name", "id"]

    def __str__(self) -> str:
        return self.name


class Result(models.Model):
    """
    対戦結果（Resultテーブル）

    カラム:
    - id: 自動採番（Django標準）
    - 日付: date
    - 使用デッキ: used_deck
    - 勝敗結果: match_result
    - 備考: note
    """

    # ユーザーごとに結果を紐付け（一覧も user でフィルタする）
    user = models.ForeignKey(
        "auth.User",
        on_delete=models.CASCADE,
        related_name="results",
        verbose_name="ユーザー",
    )
    date = models.DateField("日付")
    used_deck = models.CharField("使用デッキ", max_length=100)
    opponent_deck = models.ForeignKey(
        "dashbords.OpponentDeck",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="results",
        verbose_name="対面デッキ",
    )
    play_order = models.CharField("先行/後攻", max_length=10, blank=True, default="")
    match_result = models.CharField("勝敗結果", max_length=50)
    note = models.TextField("備考", blank=True)

    created_at = models.DateTimeField("作成日時", auto_now_add=True)
    updated_at = models.DateTimeField("更新日時", auto_now=True)

    class Meta:
        verbose_name = "Result"
        verbose_name_plural = "Results"
        ordering = ["-date", "-id"]

    def __str__(self) -> str:
        return f"{self.date} / {self.used_deck} / {self.play_order} / {self.match_result}"


