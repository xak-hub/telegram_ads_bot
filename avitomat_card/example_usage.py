"""
Пример: воспроизводит карточку ThinkPad T14 Gen 4 из чата.

Перед запуском положите вырезанное фото товара (PNG с прозрачным фоном)
рядом, например как laptop_cutout.png, либо получите его через
background_removal.remove_background() из исходного фото.
"""

from card_generator import CardConfig, Badge, Spec, generate_card

if __name__ == "__main__":
    config = CardConfig(
        product_cutout_path="laptop_cutout.png",
        title="Lenovo ThinkPad T14 Gen 4",
        specs=[
            Spec("assets/icons/spec_screen.png", "Экран 14\""),
            Spec("assets/icons/spec_cpu.png", "Intel Core i5-1335U"),
            Spec("assets/icons/spec_ram.png", "16 ГБ ОЗУ + 256 ГБ SSD"),
            Spec("assets/icons/spec_gpu.png", "Intel Iris Xe Graphics"),
        ],
        badges=[
            Badge("assets/icons/insurance.png", "Гарантия 6 месяцев"),
            Badge("assets/icons/windows.png", "Обновлённый Windows"),
            Badge("assets/icons/security.png", "Проверен по 25 параметрам"),
            Badge("assets/icons/loading.png", "Свежие драйверы, базовый софт"),
        ],
        output_path="output/thinkpad_t14.png",
    )

    result_path = generate_card(config)
    print(f"Готово: {result_path}")
