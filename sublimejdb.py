"""
Copyright (c) 2012 Fredrik Ehnbom, 2014 Jason Gardner

This software is provided 'as-is', without any express or implied
warranty. In no event will the authors be held liable for any damages
arising from the use of this software.

Permission is granted to anyone to use this software for any purpose,
including commercial applications, and to alter it and redistribute it
freely, subject to the following restrictions:

   1. The origin of this software must not be misrepresented; you must not
   claim that you wrote the original software. If you use this software
   in a product, an acknowledgment in the product documentation would be
   appreciated but is not required.

   2. Altered source versions must be plainly marked as such, and must not be
   misrepresented as being the original software.

   3. This notice may not be removed or altered from any source
   distribution.
"""
import sublime
import sublime_plugin
import subprocess
import threading
import time
import traceback
import os
import sys
import re
import queue as Queue

DEBUG = None

jdb_lastresult = ""
jdb_cursor = ""
jdb_cursor_position = 0
jdb_last_cursor_view = None
jdb_bkp_layout = {}
jdb_bkp_window = None
jdb_bkp_view = None
jdb_shutting_down = False
jdb_process = None
jdb_run_status = None

class JDBView(object):
    def __init__(self, name, s=True, settingsprefix=None):
        self.queue = Queue.Queue()
        self.name = name
        self.closed = True
        self.doScroll = s
        self.view = None
        self.settingsprefix = settingsprefix
        self.timer = None
        self.lines = ""
        self.lock = threading.RLock()

    def is_open(self):
        return not self.closed

    def open_at_start(self):
        if self.settingsprefix is not None:
            return get_setting("%s_open" % self.settingsprefix, False)
        return False

    def open(self):
        if self.view is None or self.view.window() is None:
            if self.settingsprefix is not None:
                sublime.active_window().focus_group(get_setting("%s_group" % self.settingsprefix, 0))
            self.create_view()

    def close(self):
        if self.view is not None:
            if self.settingsprefix is not None:
                sublime.active_window().focus_group(get_setting("%s_group" % self.settingsprefix, 0))
            self.destroy_view()

    def should_update(self):
        return self.is_open() and is_running() and jdb_run_status == "stopped"

    def set_syntax(self, syntax):
        if self.is_open():
            self.get_view().set_syntax_file(syntax)


    def timed_add(self):
        try:
            self.lock.acquire()
            lines = self.lines
            self.lines = ""
            self.timer = None
            self.queue.put((self.do_add_line, lines))
            sublime.set_timeout(self.update, 0)
        finally:
            self.lock.release()


    def add_line(self, line, now=True):
        if self.is_open():
            try:
                self.lock.acquire()
                self.lines += line
                if self.timer:
                    self.timer.cancel()
                if self.lines.count("\n") > 10 or now:
                    self.timed_add()
                else:
                    self.timer = threading.Timer(0.1, self.timed_add)
                    self.timer.start()
            finally:
                self.lock.release()

    def scroll(self, line):
        if self.is_open():
            self.queue.put((self.do_scroll, line))
            sublime.set_timeout(self.update, 0)

    def set_viewport_position(self, pos):
        if self.is_open():
            self.queue.put((self.do_set_viewport_position, pos))
            sublime.set_timeout(self.update, 0)

    def clear(self, now=False):
        if self.is_open():
            if not now:
                self.queue.put((self.do_clear, None))
                sublime.set_timeout(self.update, 0)
            else:
                self.do_clear(None)

    def create_view(self):
        self.view = sublime.active_window().new_file()
        self.view.set_name(self.name)
        self.view.set_scratch(True)
        self.view.set_read_only(True)
        ## Setting command_mode to false so that vintage
        ## does not eat the "enter" keybinding
        self.view.settings().set('command_mode', False)
        self.closed = False

    def destroy_view(self):
        sublime.active_window().focus_view(self.view)
        sublime.active_window().run_command("close")
        self.view = None
        self.closed = True

    def is_closed(self):
        return self.closed

    def was_closed(self):
        self.closed = True

    def fold_all(self):
        if self.is_open():
            self.queue.put((self.do_fold_all, None))

    def get_view(self):
        return self.view

    def do_add_line(self, line):
        self.view.run_command("jdb_view_add_line", {"line": line, "doScroll": self.doScroll})

    def do_fold_all(self, data):
        self.view.run_command("fold_all")

    def do_clear(self, data):
        self.view.run_command("jdb_view_clear")

    def do_scroll(self, data):
        self.view.run_command("goto_line", {"line": data + 1})

    def do_set_viewport_position(self, data):
        ## Shouldn't have to call viewport_extent, but it
        ## seems to flush whatever value is stale so that
        ## the following set_viewport_position works.
        ## Keeping it around as a WAR until it's fixed
        ## in Sublime Text 2.
        self.view.viewport_extent()
        self.view.set_viewport_position(data, False)

    def update(self):
        if not self.is_open():
            return
        try:
            while not self.queue.empty():
                cmd, data = self.queue.get()
                try:
                    cmd(data)
                finally:
                    self.queue.task_done()
        except:
            traceback.print_exc()

    def on_session_ended(self):
        if get_setting("%s_clear_on_end" % self.settingsprefix, True):
            self.clear()


class JdbViewClear(sublime_plugin.TextCommand):
    def run(self, edit):
        self.view.set_read_only(False)
        self.view.erase(edit, sublime.Region(0, self.view.size()))
        self.view.set_read_only(True)


class JdbViewAddLine(sublime_plugin.TextCommand):
    def run(self, edit, line, doScroll):
        self.view.set_read_only(False)
        self.view.insert(edit, self.view.size(), line)
        self.view.set_read_only(True)
        if doScroll:
            self.view.show(self.view.size())


class JDBVariable:
    def __init__(self, vp):
        self.name = vp[0]
        self.value = vp[1]
        # self.parent = parent
        self.children = []
        self.line = 0
        self.is_expanded = False

    def has_children(self):
        return len(self.children) > 0

    def __str__(self):
        return "%s = %s" % (self.name, self.value)

    def format(self, indent="", output="", line=0):
        icon = " "
        if self.has_children():
            if self.is_expanded:
                icon = "-"
            else:
                icon = "+"

        output += "%s%s%s\n" % (indent, icon, self)
        self.line = line
        line = line + 1
        indent += "    "
        if self.is_expanded:
            for child in self.children:
                output, line = child.format(indent, output, line)
        return (output, line)


class JDBVariablesView(JDBView):
    def __init__(self):
        super(JDBVariablesView, self).__init__("JDB Variables", False, settingsprefix="variables")
        self.variables = []

    def open(self):
        super(JDBVariablesView, self).open()
        self.set_syntax("Packages/Java/Java.tmLanguage")
        if self.is_open() and jdb_run_status == "stopped":
            self.update_variables(False)

    def update_view(self):
        output = ""
        line = 0
        for local in self.variables:
            output, line = local.format(line=line)
            self.add_line(output)
        self.update()

    def add_variable(self, exp):
        v = self.create_variable(exp)
        if v:
            self.variables.append(v)

    def create_variable(self, exp):
        parts = exp.split(" = ")
        return JDBVariable(parts)

    def clear_view(self):
        self.variables = []
        self.clear()

    def update_variables(self):
        if not self.should_update():
            return
        self.clear_view()
        result = run_cmd("locals")
        localLines = result.split("\n")
        for ll in localLines:
            if not "Method arguments:" in ll and not "Local variables:" in ll:
                parts = ll.split(" = ")
                var_vals = run_cmd("print %s" % parts[0])
                self.add_variable(var_vals)
        self.update_view()

    def get_variable_at_line(self, line, var_list=None):
        if var_list is None:
            var_list = self.variables
        if len(var_list) == 0:
            return None

        for i in range(len(var_list)):
            if var_list[i].line == line:
                return var_list[i]
            elif var_list[i].line > line:
                return self.get_variable_at_line(line, var_list[i - 1].children)
        return self.get_variable_at_line(line, var_list[len(var_list) - 1].children)


class JDBBreakpoint(object):
    def __init__(self, filename="", line=0):
        self.original_filename = normalize(filename)
        self.original_line = line
        # self.clear()
        self.add()

    @property
    def line(self):
        return self.original_line

    @property
    def filename(self):
        return normalize(self.original_filename)

    def insert(self):
        break_cmd = "stop at"
        class_name = determine_class_from_file(self.original_filename)
        cmd = "%s %s:%d" % (break_cmd, class_name, self.original_line)
        out = run_cmd(cmd)
        if "is not a valid class name" in out or "Deferring breakpoint" in out:
            sublime.error_message("%s%s" % ("Cannot locate class: ", class_name))
            return

    def add(self):
        if is_running():
            self.insert()

    def remove(self):
        if is_running():
            # TODO
            pass

    def format(self):
        return "%s:%d\n" % (self.filename, self.line)


class JDBWatch(JDBBreakpoint):
    def __init__(self, exp):
        self.exp = exp
        super(JDBWatch, self).__init__(None, -1)

    def insert(self):
        out = run_cmd("-break-watch %s" % self.exp)
        res = parse_result_line(out)
        if get_result(out) == "error":
            return

        self.number = int(res["wpt"]["number"])

    def format(self):
        return "%d - watch: %s\n" % (self.number, self.exp)


class JDBBreakpointView(JDBView):
    def __init__(self):
        super(JDBBreakpointView, self).__init__("JDB Breakpoints", s=False, settingsprefix="breakpoints")
        self.breakpoints = []

    def open(self):
        super(JDBBreakpointView, self).open()
        ## self.set_syntax("Packages/SublimeJDB/jdb_disasm.tmLanguage")
        self.get_view().settings().set("word_wrap", False)
        if self.is_open():
            self.update_view()

    def on_session_ended(self):
        ## Intentionally not calling super
        # for bkpt in self.breakpoints:
        #     bkpt.clear()
        pass

    def update_marker(self, view):
        bps = []
        fn = view.file_name()
        if fn is None:
            return
        fn = normalize(fn)
        for bkpt in self.breakpoints:
            if bkpt.filename == fn and not (bkpt.line == jdb_cursor_position and fn == jdb_cursor):
                bps.append(view.full_line(view.text_point(bkpt.line - 1, 0)))

        view.add_regions("sublimejdb.breakpoints", bps,
                            get_setting("breakpoint_scope", "keyword.jdb"),
                            get_setting("breakpoint_icon", "circle"),
                            sublime.HIDDEN)

    def find_breakpoint(self, filename, line):
        filename = normalize(filename)
        for bkpt in self.breakpoints:
            if bkpt.filename == filename and bkpt.line == line:
                return bkpt
        return None


    def toggle_breakpoint(self, filename, line):
        bkpt = self.find_breakpoint(filename, line)
        if bkpt:
            bkpt.remove()
            self.breakpoints.remove(bkpt)
        else:
            self.breakpoints.append(JDBBreakpoint(filename, line))
        self.update_view()

    def sync_breakpoints(self):
        global breakpoints
        for bkpt in self.breakpoints:
            bkpt.add()
        update_view_markers()
        self.update_view()

    def update_view(self):
        if not self.is_open():
            return
        pos = self.get_view().viewport_position()
        self.clear()
        self.breakpoints.sort(key=lambda b: (b.filename, b.line))
        for bkpt in self.breakpoints:
            self.add_line(bkpt.format())
        self.set_viewport_position(pos)
        self.update()


class JDBSessionView(JDBView):
    def __init__(self):
        super(JDBSessionView, self).__init__("JDB Session", s=False, settingsprefix="session")

    def open(self):
        super(JDBSessionView, self).open()
        self.set_syntax("Packages/SublimeJDB/jdb_session.tmLanguage")


jdb_console_view = JDBView("JDB Console", settingsprefix="console")
jdb_variables_view = JDBVariablesView()
jdb_breakpoint_view = JDBBreakpointView()
jdb_views = [jdb_console_view, jdb_variables_view, jdb_breakpoint_view]


def update_view_markers(view=None):
    if view is None:
        view = sublime.active_window().active_view()

    fn = view.file_name()
    if fn is not None:
        fn = normalize(fn)
    pos_scope = get_setting("position_scope", "entity.name.class")
    pos_icon = get_setting("position_icon", "bookmark")

    cursor = []
    if fn == jdb_cursor and jdb_cursor_position != 0:
        cursor.append(view.full_line(view.text_point(jdb_cursor_position - 1, 0)))
    global jdb_last_cursor_view
    if jdb_last_cursor_view is not None:
        jdb_last_cursor_view.erase_regions("sublimejdb.position")
    jdb_last_cursor_view = view
    view.add_regions("sublimejdb.position", cursor, pos_scope, pos_icon, sublime.HIDDEN)

    jdb_breakpoint_view.update_marker(view)


count = 0


def run_cmd(cmd, block=True):
    global count
    global jdb_lastresult
    if not is_running():
        raise ValueError("Cannot run '%s'! JDB is not running" % cmd)

    timeout = 10
    timeoutcount = timeout/0.001

    log_debug("jdb_%s: %s" % ("stdin", cmd))
    cmd = "%s\n" % cmd
    if jdb_console_view is not None:
        jdb_console_view.add_line("-> %s" % cmd, False)
    jdb_process.stdin.write(cmd.encode(sys.getdefaultencoding()))
    if block:
        count = count + 1
        countstr = "%d^" % count
        i = 0
        while not jdb_lastresult.startswith(countstr) and i < timeoutcount:
            i += 1
            time.sleep(0.001)
        if i >= timeoutcount:
            raise ValueError("Command \"%s\" took longer than %d seconds to perform?" % (cmd, timeout))
        return jdb_lastresult[len(countstr):]


def wait_until_loaded():
    i = 0
    log_debug("waiting until JDB is loaded...")
    while not jdb_loaded and i < 50:
        i = i + 1
        time.sleep(0.1)
        if i >= 50:
            return False
    log_debug("JDB is now loaded!")
    return True


def update_cursor():
    global jdb_cursor
    global jdb_cursor_position

    if jdb_run_status != "running":
        res = run_cmd("where")

        first_line = res.split("\n")[0]
        c_start = first_line.find("] ") + 2
        del_idx = first_line.find(" (")
        c_end = first_line.rfind(".", 0, del_idx)
        l_start = first_line.find(":") + 1
        l_end = first_line.find(")")

        jdb_cursor = determine_file_from_class(first_line[c_start:c_end])
        jdb_cursor_position = int(first_line[l_start:l_end])
        sublime.active_window().focus_group(get_setting("file_group", 0))
        sublime.active_window().open_file("%s:%d" % (jdb_cursor, jdb_cursor_position), sublime.ENCODED_POSITION)

        update_view_markers()
        jdb_variables_view.update_variables()


def session_ended_status_message():
    sublime.status_message("JDB session ended")


def jdboutput(pipe):
    global count
    global jdb_process
    global jdb_loaded
    global jdb_lastresult
    global jdb_run_status
    jdb_loaded = False
    prev_lines = ""
    current_line = ""
    thread_out_regex = re.compile("^Thread-\d+\[\d+\]$")

    while True:
        try:
            nextbyte = pipe.read(1)
            if not nextbyte:
                break
            nextchar = nextbyte.decode(sys.getdefaultencoding())
            countstr = "%d^" % count
            if nextchar == ">" and len(current_line) == 0:
                if jdb_loaded:
                    log_debug("jdb_%s: %s" % ("stdout" if pipe == jdb_process.stdout else "stderr", prev_lines))
                    jdb_console_view.add_line("<-%s\n" % prev_lines, False)
                    jdb_lastresult = "%s%s" % (countstr, prev_lines)
                    current_line = ""
                    prev_lines = ""
                    continue
                else:
                    current_line = ""
                    prev_lines = ""
                    jdb_loaded = True
                    continue
            if nextchar == "\n":
                if len(prev_lines) > 0:
                    prev_lines = "%s\n" % (prev_lines)
                prev_lines = "%s%s" % (prev_lines, current_line)
                current_line = ""
            else:
                current_line = current_line + nextchar

            if thread_out_regex.match(current_line) is not None:
                unsol_result = "%s%s" % (prev_lines, current_line)
                log_debug("jdb_%s: %s" % ("stdout" if pipe == jdb_process.stdout else "stderr", unsol_result))
                jdb_console_view.add_line("<-%s\n" % unsol_result, False)
                if jdb_run_status == "running":
                    jdb_run_status = "stopped"
                    sublime.set_timeout(update_cursor, 0)
                else:
                    jdb_lastresult = "%s%s" % (countstr, prev_lines)
                current_line = ""
                prev_lines = ""


        except:
            traceback.print_exc()
    if pipe == jdb_process.stdout:
        log_debug("JDB session ended")
        jdb_console_view.add_line("## JDB session ended ##\n")
        sublime.set_timeout(session_ended_status_message, 0)
    global jdb_cursor_position
    jdb_cursor_position = 0
    jdb_run_status = None
    sublime.set_timeout(update_view_markers, 0)

    for view in jdb_views:
        sublime.set_timeout(view.on_session_ended, 0)
    sublime.set_timeout(cleanup, 0)


def cleanup():
    if get_setting("close_views", True):
        for view in jdb_views:
            view.close()
    if get_setting("push_pop_layout", True):
        jdb_bkp_window.set_layout(jdb_bkp_layout)
        jdb_bkp_window.focus_view(jdb_bkp_view)


def is_running():
    return jdb_process is not None and jdb_process.poll() is None


def go_to_run_state():
    global jdb_run_status
    jdb_variables_view.clear_view()
    jdb_run_status = "running"


class JdbLaunch(sublime_plugin.WindowCommand):
    def run(self):
        global jdb_process
        global jdb_run_status
        global jdb_bkp_window
        global jdb_bkp_view
        global jdb_bkp_layout
        global jdb_shutting_down
        global DEBUG
        view = self.window.active_view()
        DEBUG = get_setting("debug", False, view)

        if jdb_process is None or jdb_process.poll() is not None:
            commandline = get_setting("commandline", view=view)
            commandline = expand_path(commandline, self.window)
            path = expand_path(get_setting("workingdir", "/tmp", view), self.window)
            log_debug("Running: %s" % commandline)
            log_debug("In directory: %s" % path)
            if commandline == "notset" or path == "notset":
                sublime.error_message("You have not configured the plugin correctly, the default configuration file and your user configuration file will open in a new window")
                sublime.run_command("new_window")
                wnd = sublime.active_window()
                wnd.set_layout({
                    "cols": [0.0, 0.5, 1.0],
                    "rows": [0, 1.0],
                    "cells": [[0,0,1,1], [1,0,2,1]],
                })
                v = wnd.open_file("%s/User/SublimeJDB.sublime-settings" % sublime.packages_path())
                v2 = wnd.open_file("%s/SublimeJDB/SublimeJDB.sublime-settings" % sublime.packages_path())
                wnd.set_view_index(v2, 1, 0)
                return
            if not os.path.exists(path):
                sublime.error_message("The directory given does not exist: %s" % path)
                return
            jdb_process = subprocess.Popen(commandline, shell=True, cwd=path,
                                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

            log_debug("Process: %s" % jdb_process)
            jdb_bkp_window = sublime.active_window()
            ##back up current layout before opening the debug one
            ##it will be restored when debug is finished
            jdb_bkp_layout = jdb_bkp_window.get_layout()
            jdb_bkp_view = jdb_bkp_window.active_view()
            jdb_bkp_window.set_layout(
                get_setting("layout",
                    {
                        "cols": [0.0, 0.5, 1.0],
                        "rows": [0.0, 0.75, 1.0],
                        "cells": [[0, 0, 2, 1], [0, 1, 1, 2], [1, 1, 2, 2]]
                    }
                )
            )

            for view in jdb_views:
                if view.is_closed() and view.open_at_start():
                    view.open()
                view.clear()

            jdb_shutting_down = False

            t = threading.Thread(target=jdboutput, args=(jdb_process.stdout,))
            t.start()
            t = threading.Thread(target=jdboutput, args=(jdb_process.stderr,))
            t.start()

            jdb_console_view.add_line("## Attaching JDB... ##\n")
            has_loaded = wait_until_loaded()
            if not has_loaded:
                sublime.error_message("JDB did not start.  Check that the Java process is running and listening and that your settings are correct")
                run_cmd("quit", False)
                return
            go_to_run_state()
            jdb_console_view.add_line("## JDB Attached ##\n")
            jdb_breakpoint_view.sync_breakpoints()
        else:
            sublime.status_message("JDB is already running!")

    def is_enabled(self):
        return not is_running()

    def is_visible(self):
        return not is_running()


class JdbContinue(sublime_plugin.WindowCommand):
    def run(self):
        global jdb_cursor_position
        jdb_cursor_position = 0
        update_view_markers()
        go_to_run_state()
        run_cmd("cont", False)

    def is_enabled(self):
        return is_running() and jdb_run_status != "running"

    def is_visible(self):
        return is_running()


class JdbExit(sublime_plugin.WindowCommand):
    def run(self):
        global jdb_shutting_down
        jdb_shutting_down = True
        # wait_until_stopped()
        run_cmd("quit", False)

    def is_enabled(self):
        return is_running()

    def is_visible(self):
        return is_running()


class JdbStepOver(sublime_plugin.WindowCommand):
    def run(self):
        go_to_run_state()
        run_cmd("next", False)

    def is_enabled(self):
        return is_running() and jdb_run_status != "running"

    def is_visible(self):
        return is_running()


class JdbStepInto(sublime_plugin.WindowCommand):
    def run(self):
        go_to_run_state()
        run_cmd("step", False)

    def is_enabled(self):
        return is_running() and jdb_run_status != "running"

    def is_visible(self):
        return is_running()


class JdbStepOut(sublime_plugin.WindowCommand):
    def run(self):
        go_to_run_state()
        run_cmd("step up", False)

    def is_enabled(self):
        return is_running() and jdb_run_status != "running"

    def is_visible(self):
        return is_running()


class JdbIgnored(sublime_plugin.WindowCommand):
    def run(self):
        pass


class JdbToggleBreakpoint(sublime_plugin.TextCommand):
    def run(self, edit):
        fn = self.view.file_name()

        if jdb_breakpoint_view.is_open() and self.view.id() == jdb_breakpoint_view.get_view().id():
            row = self.view.rowcol(self.view.sel()[0].begin())[0]
            if row < len(jdb_breakpoint_view.breakpoints):
                jdb_breakpoint_view.breakpoints[row].remove()
                jdb_breakpoint_view.breakpoints.pop(row)
                jdb_breakpoint_view.update_view()
        elif jdb_variables_view.is_open() and self.view.id() == jdb_variables_view.get_view().id():
            var = jdb_variables_view.get_variable_at_line(self.view.rowcol(self.view.sel()[0].begin())[0])
            if var is not None:
                jdb_breakpoint_view.toggle_watch(var.get_expression())
        # elif jdb_disassembly_view.is_open() and self.view.id() == jdb_disassembly_view.get_view().id():
           # for sel in self.view.sel():
           #      line = self.view.substr(self.view.line(sel))
           #      addr = re.match(r"^[^:]+", line)
           #      if addr:
           #         jdb_breakpoint_view.toggle_breakpoint_addr(addr.group(0))
        elif fn is not None:
            for sel in self.view.sel():
                line, col = self.view.rowcol(sel.a)
                jdb_breakpoint_view.toggle_breakpoint(fn, line + 1)
        update_view_markers(self.view)


class JdbEventListener(sublime_plugin.EventListener):
    def on_query_context(self, view, key, operator, operand, match_all):
        if key == "jdb_running":
            return is_running() == operand
        # elif key == "jdb_input_view":
        #     return jdb_input_view is not None and view.id() == jdb_input_view.id()
        elif key.startswith("jdb_"):
            v = jdb_variables_view
            # if key.startswith("jdb_register_view"):
            #     v = jdb_register_view
            # elif key.startswith("jdb_disassembly_view"):
            #     v = jdb_disassembly_view
            if key.endswith("open"):
                return v.is_open() == operand
            else:
                if v.get_view() is None:
                    return False == operand
                return (view.id() == v.get_view().id()) == operand
        return None

    def on_activated(self, view):
        if view.file_name() is not None:
            update_view_markers(view)

    def on_load(self, view):
        if view.file_name() is not None:
            update_view_markers(view)

    def on_close(self, view):
        for v in jdb_views:
            if v.is_open() and view.id() == v.get_view().id():
                v.was_closed()
                break


class JdbOpenConsoleView(sublime_plugin.WindowCommand):
    def run(self):
        jdb_console_view.open()

    def is_enabled(self):
        return not jdb_console_view.is_open()

    def is_visible(self):
        return not jdb_console_view.is_open()


class JdbOpenVariablesView(sublime_plugin.WindowCommand):
    def run(self):
        jdb_variables_view.open()

    def is_enabled(self):
        return not jdb_variables_view.is_open()

    def is_visible(self):
        return not jdb_variables_view.is_open()


class JdbOpenBreakpointView(sublime_plugin.WindowCommand):
    def run(self):
        jdb_breakpoint_view.open()

    def is_enabled(self):
        return not jdb_breakpoint_view.is_open()

    def is_visible(self):
        return not jdb_breakpoint_view.is_open()


def normalize(filename):
    if filename is None:
        return None
    return os.path.abspath(os.path.normcase(filename))


def log_debug(line):
    global DEBUG
    if DEBUG:
        sys.stdout.write(line + "\n")
        sys.stdout.flush()


def get_setting(key, default=None, view=None):
    try:
        if view is None:
            view = sublime.active_window().active_view()
        s = view.settings()
        if s.has("sublimejdb_%s" % key):
            return s.get("sublimejdb_%s" % key)
    except:
        pass
    return sublime.load_settings("SublimeJDB.sublime-settings").get(key, default)


def expand_path(value, window):
    if window is None:
        window = sublime.active_window()

    get_existing_files = \
        lambda m: [ path \
            for f in window.folders() \
            for path in [os.path.join(f, m.group('file'))] \
            if os.path.exists(path) \
        ]
    value = re.sub(r'\${project_path:(?P<file>[^}]+)}', lambda m: len(get_existing_files(m)) > 0 and get_existing_files(m)[0] or m.group('file'), value)
    value = re.sub(r'\${env:(?P<variable>.*)}', lambda m: os.getenv(m.group('variable')), value)
    if os.getenv("HOME"):
        value = re.sub(r'\${home}', re.escape(os.getenv('HOME')), value)
    value = re.sub(r'\${folder:(?P<file>.*)}', lambda m: os.path.dirname(m.group('file')), value)
    value = value.replace('\\', os.sep)
    value = value.replace('/', os.sep)
    return value

def determine_class_from_file(filename):
    class_name = filename.replace("\\", "/")
    src_prefix = get_setting("source_path_prefix", "/src/main/java/")
    class_name = class_name[class_name.find(src_prefix) + len(src_prefix):]
    class_name = class_name.replace("/", ".").replace(".java","")
    return class_name

def determine_file_from_class(class_name):
    project_root = sublime.active_window().project_data()['folders'][0]['path']
    src_prefix = project_root + get_setting("source_path_prefix", "/src/main/java/")
    filename = class_name.replace(".", "/")
    filename = src_prefix + filename + ".java"
    return filename