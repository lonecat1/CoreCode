#!/usr/bin/env python3
"""贪吃蛇 (Snake) - 终端版

使用 Python 标准库 curses 实现，无需额外依赖。

操作:
    方向键 / WASD : 控制移动方向
    p             : 暂停 / 继续
    r             : 游戏结束后重新开始
    q             : 退出游戏

运行:
    python snake.py
"""

import curses
import random
import time


# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------
GRID_WIDTH = 40          # 逻辑网格宽度（列数）
GRID_HEIGHT = 20         # 逻辑网格高度（行数）
INITIAL_SPEED = 0.12     # 初始每步间隔（秒），越小越快
MIN_SPEED = 0.05         # 最快速度上限
SPEED_STEP = 0.004       # 每吃一个食物加速量


def _direction(key):
    """把按键映射为 (dy, dx)。"""
    mapping = {
        curses.KEY_UP: (-1, 0),
        curses.KEY_DOWN: (1, 0),
        curses.KEY_LEFT: (0, -1),
        curses.KEY_RIGHT: (0, 1),
        ord('w'): (-1, 0),
        ord('s'): (1, 0),
        ord('a'): (0, -1),
        ord('d'): (0, 1),
        ord('W'): (-1, 0),
        ord('S'): (1, 0),
        ord('A'): (0, -1),
        ord('D'): (0, 1),
    }
    return mapping.get(key)


class SnakeGame:
    def __init__(self, stdscr):
        self.stdscr = stdscr
        self.running = True
        self.paused = False
        self.reset()

    # ------------------------------------------------------------------
    # 游戏状态
    # ------------------------------------------------------------------
    def reset(self):
        cy, cx = GRID_HEIGHT // 2, GRID_WIDTH // 2
        # 蛇身：头在前
        self.snake = [(cy, cx - i) for i in range(3)]
        self.direction = (0, 1)          # 初始向右
        self.pending_direction = (0, 1)
        self.speed = INITIAL_SPEED
        self.score = 0
        self.game_over = False
        self.place_food()

    def place_food(self):
        empty = [
            (y, x)
            for y in range(GRID_HEIGHT)
            for x in range(GRID_WIDTH)
            if (y, x) not in self.snake
        ]
        self.food = random.choice(empty) if empty else None

    # ------------------------------------------------------------------
    # 一步逻辑
    # ------------------------------------------------------------------
    def step(self):
        if self.game_over or self.paused:
            return

        # 应用上一轮缓冲的方向（避免一次按两下导致自杀掉头）
        dy, dx = self.pending_direction
        cur_dy, cur_dx = self.direction
        if (dy, dx) != (-cur_dy, -cur_dx):
            self.direction = (dy, dx)

        head_y, head_x = self.snake[0]
        new_head = (head_y + self.direction[0], head_x + self.direction[1])

        ny, nx = new_head
        # 撞墙
        if ny < 0 or ny >= GRID_HEIGHT or nx < 0 or nx >= GRID_WIDTH:
            self.game_over = True
            return
        # 撞自己（尾巴这一步会移开，所以排除最后一个节点）
        body = self.snake[:-1]
        if new_head in body:
            self.game_over = True
            return

        self.snake.insert(0, new_head)

        if new_head == self.food:
            self.score += 1
            self.speed = max(MIN_SPEED, self.speed - SPEED_STEP)
            self.place_food()
        else:
            self.snake.pop()

    # ------------------------------------------------------------------
    # 渲染
    # ------------------------------------------------------------------
    def draw(self):
        scr = self.stdscr
        scr.erase()

        # 外框
        top = "─" * (GRID_WIDTH + 2)
        for y in range(GRID_HEIGHT):
            cells = [' '] * GRID_WIDTH
            for (sy, sx) in self.snake:
                if sy == y and 0 <= sx < GRID_WIDTH:
                    cells[sx] = '█'
            if self.food and self.food[0] == y:
                fx = self.food[1]
                if 0 <= fx < GRID_WIDTH and cells[fx] == ' ':
                    cells[fx] = '●'
            line = "│" + "".join(cells) + "│"
            self._safe_addstr(y + 1, 0, line)

        self._safe_addstr(0, 0, top)
        self._safe_addstr(GRID_HEIGHT + 1, 0, top)

        # 状态栏
        status = f" Score: {self.score}   Speed: {round(1/self.speed)} fps "
        help_line = " Arrows/WASD move · P pause · Q quit · R restart "
        self._safe_addstr(GRID_HEIGHT + 2, 0, status.ljust(GRID_WIDTH + 2))
        self._safe_addstr(GRID_HEIGHT + 3, 0, help_line.ljust(GRID_WIDTH + 2))

        if self.paused:
            msg = " PAUSED - press P to resume "
            self._centered(str(GRID_HEIGHT // 2), msg, curses.color_pair(2))
        elif self.game_over:
            msg = f" GAME OVER  score={self.score}  press R to restart, Q to quit "
            self._centered(str(GRID_HEIGHT // 2), msg, curses.color_pair(3))

        scr.refresh()

    def _centered(self, row, text, attr=0):
        width = self.stdscr.getmaxyx()[1]
        x = max(0, (width - len(text)) // 2)
        try:
            self.stdscr.addstr(row, x, text[:width - x], attr)
        except curses.error:
            pass

    def _safe_addstr(self, y, x, text, attr=0):
        max_y, max_x = self.stdscr.getmaxyx()
        if y >= max_y:
            return
        text = text[:max_x - x]
        try:
            self.stdscr.addstr(y, x, text, attr)
        except curses.error:
            pass

    # ------------------------------------------------------------------
    # 主循环
    # ------------------------------------------------------------------
    def handle_key(self, key):
        if key in (ord('q'), ord('Q'), 27):
            self.running = False
            return

        d = _direction(key)
        if d is not None:
            self.pending_direction = d
            return

        if key in (ord('p'), ord('P')):
            if not self.game_over:
                self.paused = not self.paused
        elif key in (ord('r'), ord('R')):
            if self.game_over:
                self.reset()

    def run(self):
        last = time.time()
        while self.running:
            now = time.time()
            if now - last >= self.speed:
                self.step()
                last = now
            self.draw()

            try:
                k = self.stdscr.getch()
            except KeyboardInterrupt:
                break
            if k != -1:
                self.handle_key(k)
                # 暂停/结束时阻塞等待按键，避免空转
                if self.paused or self.game_over:
                    self.stdscr.nodelay(False)
                    k2 = self.stdscr.getch()
                    self.stdscr.nodelay(True)
                    self.handle_key(k2)
                    last = time.time()


def main(stdscr):
    curses.curs_set(0)
    stdscr.nodelay(True)
    stdscr.keypad(True)

    # 颜色
    if curses.has_colors():
        curses.start_color()
        curses.use_default_colors()
        curses.init_pair(1, curses.COLOR_GREEN, -1)   # 蛇
        curses.init_pair(2, curses.COLOR_YELLOW, -1)  # 暂停
        curses.init_pair(3, curses.COLOR_RED, -1)     # 结束

    game = SnakeGame(stdscr)
    game.run()


if __name__ == "__main__":
    try:
        curses.wrapper(main)
    except KeyboardInterrupt:
        pass
