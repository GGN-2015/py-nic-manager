def main() -> None:
    try:
        from .qt_app import main as qt_main
    except ImportError:
        from .app import main as tkinter_main

        tkinter_main()
        return
    qt_main()


if __name__ == "__main__":
    main()
