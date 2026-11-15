<?php

namespace App\Plugins\BackupRestore\Services;

class Formatter
{
    public static function bytes(?int $bytes): string
    {
        $bytes = (int) $bytes;
        if ($bytes < 1024) {
            return $bytes . ' B';
        }
        $units = ['KiB', 'MiB', 'GiB', 'TiB'];
        $value = $bytes;
        foreach ($units as $unit) {
            $value /= 1024;
            if ($value < 1024) {
                return number_format($value, 1) . ' ' . $unit;
            }
        }

        return number_format($value, 1) . ' PiB';
    }

    public static function duration(?int $seconds): string
    {
        if ($seconds === null) {
            return '-';
        }
        $seconds = (int) $seconds;
        if ($seconds < 60) {
            return $seconds . 's';
        }
        if ($seconds < 3600) {
            return intdiv($seconds, 60) . 'm ' . ($seconds % 60) . 's';
        }

        return intdiv($seconds, 3600) . 'h ' . intdiv($seconds % 3600, 60) . 'm';
    }
}
