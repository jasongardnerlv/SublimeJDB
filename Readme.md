# SublimeJDB #

## Description ##
JDB plugin for Sublime Text. [Loosely based on the Sublime Text GDB plugin](https://github.com/quarnster/SublimeGDB)

## Usage ##
**This is a work in progress.**

So far, this has only been tested on **OSX Mavericks** with **Sublime Text 3** and **Java 1.7.0_45**.

Until this project is made available via *Package Control*, you'll need to create a **SublimeJDB** folder in your
Sublime Text **Packages** directory (in OSX - *~/Library/Application Support/Sublime Text 3/Packages*) and add the
contents of this repository.

Edit the *SublimeJDB.sublime-settings*.  In particular,

- **commandline** - Set to the command line string that will be used to launch JDB
- **source_path_prefix** - Set to the folder structure that occurs between your project's root and where the Java package starts, usually "/src/main/java/"

To debug:

- Make sure your Java app is running and listening on the port used in the *commandline* setting
- Make sure the Java files you will be debugging are part of a Sublime Text project
- **Start the JDB session**:
  - via keyboard: "cmd+." and then "cmd+r"
  - via right-click: JDB -> Start Debugging
  - via command: "cmd+shift+p" -> "SublimeJDB: Start Debugging"
- With cursor on the desired line, **add/remove a breakpoint**:
  - via keyboard: "cmd+." and then "cmd+b"
  - via right-click: JDB -> Toggle Breakpoint
- When the breakpoint is hit:
  - **Step Into**
    - via keyboard: "cmd+." and then "cmd+i"
    - via right-click: JDB -> Step Into
    - via command: "cmd+shift+p" -> "SublimeJDB: Step Into"
  - **Step Over**
    - via keyboard: "cmd+." and then "cmd+o"
    - via right-click: JDB -> Step Over
    - via command: "cmd+shift+p" -> "SublimeJDB: Step Over"
  - **Step Out**
    - via keyboard: "cmd+." and then "cmd+u"
    - via right-click: JDB -> Step Out
    - via command: "cmd+shift+p" -> "SublimeJDB: Step Out"
  - **Continue** (resume)
    - via keyboard: "cmd+." and then "cmd+r"
    - via right-click: JDB -> Continue
    - via command: "cmd+shift+p" -> "SublimeJDB: Continue"
- **Stop the JDB session**:
  - via keyboard: "cmd+." and then "cmd+x"
  - via right-click: JDB -> Stop Debugging
  - via command: "cmd+shift+p" -> "SublimeJDB: Stop Debugging"