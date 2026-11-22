#ifdef _WIN32
int wmain(int argc, wchar_t** argv) {
#else
int main(int argc, char** argv) {
#endif
    PyConfig config;
    PyConfig_InitPythonConfig(&config);
    config.parse_argv = 0;
#ifdef _WIN32
    PyStatus status = PyConfig_SetArgv(&config, argc, argv);
#else
    PyStatus status = PyConfig_SetBytesArgv(&config, argc, argv);
#endif
#ifdef CODE_TRANSLATOR_PYTHON_EXECUTABLE
    if (!PyStatus_Exception(status)) {
        status = PyConfig_SetString(&config, &config.program_name, CODE_TRANSLATOR_PYTHON_EXECUTABLE);
    }
    if (!PyStatus_Exception(status)) {
        status = PyConfig_SetString(&config, &config.executable, CODE_TRANSLATOR_PYTHON_EXECUTABLE);
    }
#endif
    if (!PyStatus_Exception(status)) status = Py_InitializeFromConfig(&config);
    PyConfig_Clear(&config);
    if (PyStatus_Exception(status)) Py_ExitStatusException(status);

    int exit_code = 0;
    try {
        auto file = source_filename();
        auto path = source_directory();
        auto argument = source_argument();
        auto* script_argv = PySys_GetObject("argv");
        py::check(PyList_SetItem(script_argv, 0, argument.release()));
#ifdef CODE_TRANSLATOR_PYTHON_EXECUTABLE
        auto original_argv = py::checked(PyList_GetSlice(script_argv, 0, PyList_GET_SIZE(script_argv)));
        py::check(PyList_Insert(original_argv.get(), 0, PySys_GetObject("executable")));
        py::check(PySys_SetObject("orig_argv", original_argv.get()));
#endif
        run_program(py::setup_module(file, path), file);
    } catch (const py::Error&) {
        PyErr_Print();
        exit_code = 1;
    }
    if (Py_FinalizeEx() < 0 && exit_code == 0) exit_code = 120;
    return exit_code;
}
