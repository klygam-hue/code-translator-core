#define PY_SSIZE_T_CLEAN
#include <Python.h>

#include <initializer_list>
#include <limits>
#include <utility>

namespace py {

struct Error {};

// A Value owns one CPython reference. A default-constructed Value is unbound.
class Value {
public:
    Value() = default;
    explicit Value(PyObject* owned) noexcept : object_(owned) {}
    Value(const Value& other) noexcept : object_(other.object_) { Py_XINCREF(object_); }
    Value(Value&& other) noexcept : object_(other.release()) {}
    ~Value() { Py_XDECREF(object_); }

    Value& operator=(Value other) noexcept {
        std::swap(object_, other.object_);
        return *this;
    }

    PyObject* get() const noexcept { return object_; }
    PyObject* release() noexcept { return std::exchange(object_, nullptr); }

private:
    PyObject* object_ = nullptr;
};

inline Value checked(PyObject* owned) {
    if (!owned) throw Error{};
    return Value(owned);
}

inline void check(int status) {
    if (status < 0) throw Error{};
}

inline Value borrow(PyObject* object) { return Value(Py_NewRef(object)); }
inline Value none() { return borrow(Py_None); }
inline Value boolean(bool value) { return borrow(value ? Py_True : Py_False); }
inline Value integer(const char* value) { return checked(PyLong_FromString(value, nullptr, 16)); }

inline Value unicode(const char* bytes, Py_ssize_t size) {
    return checked(PyUnicode_DecodeUTF8(bytes, size, "surrogatepass"));
}

inline bool truth(const Value& value) {
    const int result = PyObject_IsTrue(value.get());
    check(result);
    return result != 0;
}

inline Value load_global(PyObject* globals, const char* name) {
    auto key = checked(PyUnicode_FromString(name));
    if (auto* object = PyDict_GetItemWithError(globals, key.get())) return borrow(object);
    if (PyErr_Occurred()) throw Error{};
    auto* builtins = PyEval_GetBuiltins();
    if (auto* object = PyDict_GetItemWithError(builtins, key.get())) return borrow(object);
    if (!PyErr_Occurred()) PyErr_Format(PyExc_NameError, "name '%s' is not defined", name);
    throw Error{};
}

inline Value load_local(const Value& variable, const char* name) {
    if (variable.get()) return variable;
    PyErr_Format(PyExc_UnboundLocalError,
                 "cannot access local variable '%s' where it is not associated with a value", name);
    throw Error{};
}

inline void store_global(PyObject* globals, const char* name, const Value& value) {
    check(PyDict_SetItemString(globals, name, value.get()));
}

inline Value tuple(std::initializer_list<Value> items) {
    auto container = checked(PyTuple_New(static_cast<Py_ssize_t>(items.size())));
    Py_ssize_t index = 0;
    for (const auto& item : items) PyTuple_SET_ITEM(container.get(), index++, Py_NewRef(item.get()));
    return container;
}

inline Value list(std::initializer_list<Value> items) {
    auto container = checked(PyList_New(static_cast<Py_ssize_t>(items.size())));
    Py_ssize_t index = 0;
    for (const auto& item : items) PyList_SET_ITEM(container.get(), index++, Py_NewRef(item.get()));
    return container;
}

inline Value set(std::initializer_list<Value> items) {
    auto container = checked(PySet_New(nullptr));
    for (const auto& item : items) check(PySet_Add(container.get(), item.get()));
    return container;
}

inline Value dict(std::initializer_list<std::pair<Value, Value>> items) {
    auto container = checked(PyDict_New());
    for (const auto& item : items) {
        check(PyDict_SetItem(container.get(), item.first.get(), item.second.get()));
    }
    return container;
}

inline Value join_strings(std::initializer_list<Value> items) {
    auto separator = unicode("", 0);
    auto values = tuple(items);
    return checked(PyUnicode_Join(separator.get(), values.get()));
}

inline Value next(const Value& iterator) {
    auto* item = PyIter_Next(iterator.get());
    if (!item && PyErr_Occurred()) throw Error{};
    return Value(item);
}

class RecursionGuard {
public:
    RecursionGuard() { check(Py_EnterRecursiveCall(" while calling a translated function")); }
    ~RecursionGuard() { Py_LeaveRecursiveCall(); }
    RecursionGuard(const RecursionGuard&) = delete;
    RecursionGuard& operator=(const RecursionGuard&) = delete;
};

inline PyObject* setup_module(const Value& file, const Value& directory) {
    auto* main_module = PyImport_AddModule("__main__");
    if (!main_module) throw Error{};
    auto* globals = PyModule_GetDict(main_module);
    store_global(globals, "__file__", file);
    store_global(globals, "__cached__", none());

    auto safe_path = checked(PyObject_GetAttrString(PySys_GetObject("flags"), "safe_path"));
    if (!truth(safe_path)) {
        auto* path = PySys_GetObject("path");
        if (path) check(PyList_Insert(path, 0, directory.get()));
    }
    auto machinery = checked(PyImport_ImportModule("importlib.machinery"));
    auto loader_type = checked(PyObject_GetAttrString(machinery.get(), "SourceFileLoader"));
    auto name = unicode("__main__", 8);
    auto loader = checked(PyObject_CallFunctionObjArgs(loader_type.get(), name.get(), file.get(), nullptr));
    store_global(globals, "__loader__", loader);
    return globals;
}

inline void run_embedded(PyObject* globals, const Value& source, const Value& file) {
    auto compile = load_global(globals, "compile");
    auto mode = unicode("exec", 4);
    auto code = checked(PyObject_CallFunctionObjArgs(compile.get(), source.get(), file.get(), mode.get(), nullptr));
    checked(PyEval_EvalCode(code.get(), globals, globals));
}

}  // namespace py
