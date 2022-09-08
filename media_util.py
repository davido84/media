def gigabyte_string(n: int) -> str:
    one_kb = 1024
    if n >= one_kb ** 3:
        return f'{n * float(one_kb ** -3):.2f} GB'
    elif n >= one_kb ** 2:
        return f'{n * float(one_kb ** -2):.2f} MB'
    else:
        return f'{n * float(one_kb ** -1):.2f} KB'


class MediaException(Exception):
    pass