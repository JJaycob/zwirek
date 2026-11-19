# Zapasowy harmonogram PB95 (cron-job.org)

GitHub Actions pozostaje głównym wykonawcą. Ten zapasowy zegar uruchamia ten sam workflow przez `workflow_dispatch`, więc zadziała także wtedy, gdy GitHub pominie swoje zdarzenie `schedule`.

## Co trzeba skonfigurować jednorazowo

### 1. Utwórz ograniczony token GitHuba

W GitHub: Settings → Developer settings → Personal access tokens → Fine-grained tokens.

Ustaw:
- Repository access: Only select repositories → `JJaycob/zwirek`
- Repository permissions → Actions: Read and write
- Ustaw termin wygaśnięcia (np. 90 dni) i zapisz token w bezpiecznym miejscu.

Token daje wyłącznie dostęp do wywoływania workflow w wybranym repozytorium. Nie wklejaj go do repozytorium ani do rozmowy.

### 2. Utwórz dwa zadania w cron-job.org

Załóż / otwórz konto na https://cron-job.org i utwórz dwa zadania HTTP. W obu użyj tych samych ustawień:

**URL**
`https://api.github.com/repos/JJaycob/zwirek/actions/workflows/paliwo.yml/dispatches`

**Metoda:** POST

**Nagłówki**
```
Accept: application/vnd.github+json
Authorization: Bearer TWÓJ_FINE_GRAINED_GITHUB_TOKEN
X-GitHub-Api-Version: 2022-11-28
Content-Type: application/json
```

**Treść żądania**
```json
{"ref":"main"}
```

Włącz powiadomienia cron-job.org o nieudanych wywołaniach.

**Zadanie A — początek okna**
- Strefa: `Europe/Warsaw`
- Godziny: `14`
- Minuty: `37, 47, 57`
- Dni miesiąca, miesiące i dni tygodnia: wszystkie

**Zadanie B — kolejne kontrole**
- Strefa: `Europe/Warsaw`
- Godziny: `15, 16, 17, 18`
- Minuty: `7, 17, 27, 37, 47, 57`
- Dni miesiąca, miesiące i dni tygodnia: wszystkie

To daje zapasowe uruchomienie co 10 minut w oknie publikacji. Powtórzenia są bezpieczne: workflow ma wspólną blokadę współbieżności, pobiera najnowszy zapis stanu i nie wysyła drugi raz raportu dla tej samej daty.

### 3. Test po utworzeniu

W cron-job.org uruchom ręcznie jedno zadanie („Run now”). Powinno ono wywołać workflow w GitHub Actions. W historii Actions sprawdź, czy zdarzenie ma typ `workflow_dispatch` i zakończyło się poprawnie.

## Ważne

Zapasowy harmonogram nie potrzebuje tokenu bota Telegram — korzysta z istniejących sekretów `BOT_TOKEN` i `CHAT_ID) w GitHub. Token GitHuba znajduje się wyłącznie w konfiguracji cron-job.org. Gdy token wygaśnie, trzeba wygenerować nowy i podmienić go w nagłówku obu zadań.
