# Руководство по участию | Contributing Guide

🇷🇺 **Мы рады вашему вкладу!** Этот проект живёт благодаря сообществу.

🇬🇧 **We welcome your contributions!** This project thrives thanks to the community.

---

## 🚀 Как начать | Getting Started

1. Форкните репозиторий | Fork the repo
2. Создайте ветку | Create a branch: `git checkout -b feature/your-feature`
3. Установите зависимости | Install dependencies (см. README.md)
4. Внесите изменения | Make your changes
5. Запустите тесты | Run tests
6. Создайте PR | Open a Pull Request

## 🧪 Code quality

```bash
# Backend
cd backend
pip install -e ".[dev]"
ruff check .
mypy .

# Frontend
cd frontend
npm run lint
```

## 📐 Convention rules

- **Python**: следовуйте PEP 8, используйте type hints, docstrings на русском или английском
- **TypeScript**: strict mode, ESLint, Prettier
- **Commits**: Conventional Commits (`feat:`, `fix:`, `docs:`, `refactor:`, etc.)
- **Branches**: `feature/...`, `fix/...`, `docs/...`

## 🎯 Приоритетные направления | Priority Areas

- [ ] Парсинг источников права РК (adilet.zan.kz, zan.gov.kz и др.)
- [ ] AI-агенты (Router, Legal Expert, Statute Researcher и др.)
- [ ] Казахский язык (i18n, переводы)
- [ ] UI/UX дизайн
- [ ] Тесты и тестовые кейсы
- [ ] Документация

## 📋 Issue и PR

- **Bug**: используйте шаблон Bug Report
- **Feature**: используйте шаблон Feature Request
- **PR**: описывайте что и почему сделали, ссылайтесь на issue

## 🤔 Вопросы

Открывайте [Discussion](https://github.com/your-org/legal-ai-agent/discussions) или пишите в Telegram.

---

Спасибо за ваш вклад! ❤️
