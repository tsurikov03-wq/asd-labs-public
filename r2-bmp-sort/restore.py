"""Восстановление 24-битного BMP естественной сортировкой слиянием."""

import argparse
import struct
import sys
import tempfile
from pathlib import Path
from time import perf_counter_ns


def bmp_info(data):
    """Возвращает смещение пикселей, ширину, число строк и длину строки BMP."""
    if len(data) < 2:
        raise ValueError("Пустой или усечённый BMP-файл.")
    if data[:2] != b"BM":
        raise ValueError("Неверная сигнатура BMP: ожидалось BM.")
    if len(data) < 54:
        raise ValueError("Заголовок BMP усечён.")

    file_size = struct.unpack_from("<I", data, 2)[0]
    offset, dib_size, width, height, planes, bits, compression = (
        struct.unpack_from("<IIiiHHI", data, 10)
    )
    if dib_size < 40 or offset < 14 + dib_size:
        raise ValueError("Некорректный размер заголовка или смещение пикселей BMP.")
    if bits != 24:
        raise ValueError(f"Ожидалось 24 бита на пиксель, получено {bits}.")
    if compression != 0 or planes != 1:
        raise ValueError("Ожидался несжатый BMP с одной плоскостью.")
    if width <= 0 or height == 0:
        raise ValueError("Некорректные размеры изображения.")

    rows = abs(height)
    stride = (width * 3 + 3) // 4 * 4
    pixels_end = offset + rows * stride
    if pixels_end > len(data) or file_size > len(data):
        raise ValueError("BMP усечён: данных для всех строк изображения недостаточно.")
    if file_size < pixels_end:
        raise ValueError("Размер файла в заголовке меньше размера данных BMP.")
    return offset, width, rows, stride


def read_records(keys_path, data, info):
    """Читает пары (ключ, пиксель) в порядке хранения BMP."""
    offset, width, rows, stride = info
    count = width * rows
    records = []

    with keys_path.open(encoding="utf-8-sig") as keys:
        try:
            declared_count = int(keys.readline())
        except ValueError as error:
            raise ValueError("Первая строка файла ключей должна содержать число N.") from error

        if declared_count != count:
            raise ValueError(
                f"Число ключей не совпадает с числом пикселей: {declared_count} и {count}."
            )

        for row in range(rows):
            start = offset + row * stride
            for pos in range(start, start + width * 3, 3):
                line = keys.readline()
                if not line:
                    raise ValueError(f"Ключей меньше {count}: прочитано {len(records)}.")
                try:
                    key = int(line)
                except ValueError as error:
                    line_number = len(records) + 2
                    raise ValueError(f"В строке {line_number} ожидался целый ключ.") from error
                records.append((key, data[pos:pos + 3]))

        if any(line.strip() for line in keys):
            raise ValueError(f"В файле больше {count} ключей.")

    return records


def merge_runs(source, target, start, middle, end):
    """Сливает две соседние серии. При равенстве выбирает левую запись."""
    left = start
    right = middle
    pos = start

    while left < middle and right < end:
        if source[left][0] <= source[right][0]:
            target[pos] = source[left]
            left += 1
        else:
            target[pos] = source[right]
            right += 1
        pos += 1

    if left < middle:
        target[pos:end] = source[left:middle]
    else:
        target[pos:end] = source[right:end]


def natural_merge_sort(records):
    """Возвращает упорядоченный список, используя входной список как рабочий."""
    size = len(records)
    if size < 2:
        return records

    # Границы максимальных неубывающих серий.
    boundaries = [0]
    for pos in range(1, size):
        if records[pos - 1][0] > records[pos][0]:
            boundaries.append(pos)
    boundaries.append(size)

    if len(boundaries) == 2:
        return records

    source = records
    target = [None] * size
    while len(boundaries) > 2:
        next_boundaries = [0]
        for index in range(0, len(boundaries) - 2, 2):
            start, middle, end = boundaries[index:index + 3]
            merge_runs(source, target, start, middle, end)
            next_boundaries.append(end)

        # Последняя серия переносится целиком, если для неё нет пары.
        if next_boundaries[-1] != size:
            start = next_boundaries[-1]
            target[start:size] = source[start:size]
            next_boundaries.append(size)

        source, target = target, source
        boundaries = next_boundaries

    return source


def timed_sort(records, repeats):
    measurements = []
    for attempt in range(repeats):
        copy = records.copy()
        start = perf_counter_ns()
        result = natural_merge_sort(copy)
        elapsed_ms = (perf_counter_ns() - start) / 1_000_000
        measurements.append((elapsed_ms, None))
        if attempt + 1 < repeats:
            del result, copy

    # Та же реализация сортирует и короткий список замеров для медианы.
    measurements = natural_merge_sort(measurements)
    left_middle = measurements[(repeats - 1) // 2][0]
    right_middle = measurements[repeats // 2][0]
    return result, (left_middle + right_middle) / 2


def write_bmp(path, data, info, records):
    offset, width, rows, stride = info
    output = bytearray(data)
    index = 0

    for row in range(rows):
        start = offset + row * stride
        for pos in range(start, start + width * 3, 3):
            output[pos:pos + 3] = records[index][1]
            index += 1

    stream = tempfile.NamedTemporaryFile(dir=path.parent, delete=False)
    temporary_path = Path(stream.name)
    try:
        with stream:
            stream.write(output)
        temporary_path.replace(path)
    finally:
        temporary_path.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bmp", required=True, type=Path, help="перемешанный BMP")
    parser.add_argument("--keys", required=True, type=Path, help="текстовый файл ключей")
    parser.add_argument("--out", required=True, type=Path, help="восстановленный BMP")
    parser.add_argument("--bench", type=int, metavar="N", help="число повторов замера")
    args = parser.parse_args()
    if args.bench is not None and args.bench < 1:
        parser.error("--bench должен быть положительным целым числом")

    try:
        data = args.bmp.read_bytes()
        info = bmp_info(data)
        records = read_records(args.keys, data, info)

        if args.bench is None:
            result = natural_merge_sort(records)
        else:
            result, median_ms = timed_sort(records, args.bench)

        write_bmp(args.out, data, info, result)
    except UnicodeError:
        print("Ошибка: файл ключей должен быть текстом в UTF-8.", file=sys.stderr)
        return 1
    except (OSError, ValueError, MemoryError) as error:
        message = "Недостаточно памяти для обработки входных данных." if isinstance(
            error, MemoryError
        ) else str(error)
        print(f"Ошибка: {message}", file=sys.stderr)
        return 1

    if args.bench is not None:
        print(f"Медиана времени сортировки: {median_ms:.3f} мс")
    print(f"Изображение сохранено: {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
