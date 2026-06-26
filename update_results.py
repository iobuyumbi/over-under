#!/usr/bin/env python3
"""
Result Updater - User-friendly script to update prediction results.
Easily mark wins, losses, and pushes.
"""

import os
import json
from datetime import datetime
from prediction_tracker import get_pending_predictions, update_result, load_history

def clear_screen():
    """Clear the terminal screen."""
    os.system('cls' if os.name == 'nt' else 'clear')

def print_predictions_list(predictions):
    """Print a list of pending predictions with indices."""
    if not predictions:
        print("✅ No pending predictions!")
        return False
    
    print("=" * 80)
    print(f"📋 PENDING PREDICTIONS ({len(predictions)} total)")
    print("=" * 80)
    
    for idx, pick in enumerate(predictions, 1):
        print(f"\n[{idx}] {pick['date']} - {pick['league']}")
        print(f"      {pick['home_team']} vs {pick['away_team']}")
        if pick['type'] == 'home_win':
            print(f"      Prediction: Home Win ({pick['confidence']})")
        else:
            print(f"      Prediction: {pick['prediction'].upper()} 2.5 ({pick['confidence']})")
        print("-" * 80)
    
    return True

def update_interactive():
    """Interactive result update interface."""
    clear_screen()
    print("=" * 80)
    print("🎯 PREDICTION RESULT UPDATER")
    print("=" * 80)
    print("\nThis script helps you mark prediction results easily!")
    print("\nInstructions:")
    print("- Enter the number of the prediction to update")
    print("- Then enter 'w' for win, 'l' for loss, 'p' for push")
    print("- Enter 'q' to quit, 'r' to refresh, 'h' for help")
    
    while True:
        pending = get_pending_predictions()
        
        print("\n" + "=" * 80)
        if not print_predictions_list(pending):
            break
        
        print("\nWhat would you like to do?")
        print("  1-99: Update prediction by number")
        print("  r: Refresh list")
        print("  h: Help")
        print("  q: Quit")
        
        choice = input("\nEnter your choice: ").strip().lower()
        
        if choice == 'q':
            print("\n👋 Goodbye!")
            break
        elif choice == 'r':
            continue
        elif choice == 'h':
            print("\n📖 HELP:")
            print("  w: Win - Prediction was correct")
            print("  l: Loss - Prediction was incorrect")
            print("  p: Push - Draw or exact 2 goals (for over/under)")
            print("  r: Refresh the list of pending predictions")
            continue
        elif choice.isdigit():
            idx = int(choice) - 1
            if 0 <= idx < len(pending):
                pick = pending[idx]
                print(f"\nUpdating: {pick['home_team']} vs {pick['away_team']}")
                print(f"Date: {pick['date']}")
                
                result_choice = input("Enter result (w=win, l=loss, p=push): ").strip().lower()
                
                result_map = {
                    'w': 'win',
                    'l': 'loss',
                    'p': 'push'
                }
                
                if result_choice in result_map:
                    result = result_map[result_choice]
                    if update_result(
                        pick["date"],
                        pick["home_team"],
                        pick["away_team"],
                        result,
                        pick["type"],
                    ):
                        print(f"✅ Updated prediction as: {result.upper()}")
                    else:
                        print("❌ Failed to update prediction")
                else:
                    print("❌ Invalid choice")
            else:
                print("❌ Invalid number")
        else:
            print("❌ Invalid choice")

def show_history():
    """Show overall prediction history."""
    history = load_history()
    
    print("\n" + "=" * 80)
    print("📊 OVERALL HISTORY")
    print("=" * 80)
    
    print("\n🏠 HOME WIN PREDICTIONS")
    print("-" * 40)
    hw_stats = calculate_stats(history["home_win"])
    print_stats(hw_stats)
    
    print("\n🔥 OVER/UNDER 2.5 GOALS")
    print("-" * 40)
    ou_stats = calculate_stats(history["over_under"])
    print_stats(ou_stats)

def calculate_stats(picks):
    """Calculate statistics from a list of picks."""
    stats = {
        "total": len(picks),
        "wins": 0,
        "losses": 0,
        "pushes": 0,
        "pending": 0,
        "win_rate": 0.0
    }
    
    for pick in picks:
        if pick["result"] == "win":
            stats["wins"] += 1
        elif pick["result"] == "loss":
            stats["losses"] += 1
        elif pick["result"] == "push":
            stats["pushes"] += 1
        else:
            stats["pending"] += 1
    
    total_decisions = stats["wins"] + stats["losses"]
    if total_decisions > 0:
        stats["win_rate"] = (stats["wins"] / total_decisions) * 100
    
    return stats

def print_stats(stats):
    """Print statistics."""
    print(f"Total: {stats['total']} picks")
    print(f"Wins: {stats['wins']}")
    print(f"Losses: {stats['losses']}")
    print(f"Pushes: {stats['pushes']}")
    print(f"Pending: {stats['pending']}")
    if stats['wins'] + stats['losses'] > 0:
        print(f"Win Rate: {stats['win_rate']:.1f}%")

def main():
    """Main menu function."""
    clear_screen()
    
    while True:
        print("\n" + "=" * 80)
        print("🎯 PREDICTION RESULT MANAGER")
        print("=" * 80)
        print("\nWhat would you like to do?")
        print("1. Update pending results (interactive)")
        print("2. View overall history")
        print("3. Exit")
        
        choice = input("\nEnter choice (1-3): ").strip()
        
        if choice == "1":
            update_interactive()
        elif choice == "2":
            show_history()
        elif choice == "3":
            print("\n👋 Goodbye!")
            break
        else:
            print("\n❌ Invalid choice!")

if __name__ == "__main__":
    main()
